"""Automatic strategy-data publication through SRT and DFLS."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import shutil
import time
from uuid import uuid4

import pandas as pd
from dataflows import DataRequest, DataStatus, Dataflows, Dataset
from strategy_runtime import (
    DeploymentSpec,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    write_publication,
)

from .audit import AuditRecorder


class DataPublicationError(RuntimeError):
    pass


def seed_runtime_data(source: Path, target: Path, symbol: str) -> None:
    """Copy the tracked validated generation once as a runtime bootstrap."""
    code = symbol.upper().split(".", 1)[0]
    target = Path(target)
    if (target / f"{code}_manifest.json").is_file():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in Path(source).glob(f"{code}_*"):
        if path.is_file():
            shutil.copy2(path, target / path.name)


class AccountDataPublisher:
    """Publish every distinct instrument currently owned by a virtual account."""

    def __init__(
        self,
        *,
        store,
        repo_root: Path,
        data_dir: Path,
        start_date: str,
        audit: AuditRecorder | None = None,
        dataflows: Dataflows | None = None,
    ) -> None:
        self.store = store
        self.repo_root = repo_root
        self.data_dir = data_dir
        self.start_date = start_date
        self.audit = audit
        self.dataflows = dataflows or Dataflows()

    def publication_target(self, as_of: date) -> str:
        """Latest mainland session, using the vendor calendar including closed days."""
        start = as_of - timedelta(days=31)
        result = self._ready(self.dataflows.fetch(DataRequest(
            Dataset.TRADING_CALENDAR, "SSE", start.isoformat(), as_of.isoformat(),
            as_of.isoformat(), options={"env_file": str(Path(self.repo_root) / ".env")},
        )), "publication trading calendar")
        frame = result.dataframe
        dates = pd.DatetimeIndex(pd.to_datetime(frame["Date"], errors="raise"))
        expected = pd.date_range(start, as_of, freq="D")
        if not dates.equals(expected) or not frame["IsOpen"].isin([0, 1]).all():
            raise DataPublicationError("publication trading calendar is incomplete or invalid")
        sessions = dates[frame["IsOpen"].eq(1).to_numpy()]
        if sessions.empty:
            raise DataPublicationError("publication trading calendar has no open session")
        return sessions[-1].date().isoformat()

    @staticmethod
    def _release(repo_root: Path, strategy_id: str, version: str) -> StrategyRelease:
        path = Path(repo_root) / "strategies" / strategy_id / "versions" / f"{version}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataPublicationError(f"cannot load frozen strategy {strategy_id}-{version}: {exc}") from exc
        return StrategyRelease.from_mapping(payload)

    @staticmethod
    def _declared_symbol(repo_root: Path, strategy_id: str) -> str:
        path = Path(repo_root) / "strategies" / strategy_id / "family.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            scope = payload["scope"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DataPublicationError(f"cannot read strategy scope for {strategy_id}: {exc}") from exc
        if not isinstance(scope, list) or len(scope) != 1 or not isinstance(scope[0], str):
            raise DataPublicationError(f"{strategy_id}: runtime publication requires one symbol")
        return scope[0].upper()

    @staticmethod
    def _ready(result, label: str):
        if result.status is not DataStatus.READY:
            detail = result.error.message if result.error else result.status.value
            raise DataPublicationError(f"{label}: {result.status.value}: {detail}")
        return result

    @staticmethod
    def _write_yearly(
        frame: pd.DataFrame,
        staging: Path,
        code: str,
        label: str,
        frequency: str,
    ) -> dict[str, object]:
        source = frame.copy()
        dates = pd.to_datetime(source["Date"], errors="raise")
        date_name = "datetime" if frequency != "daily" and frequency != "weekly" else "date"
        source = source.rename(
            columns={
                "Date": date_name,
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
                "Amount": "amount",
            }
        )
        source[date_name] = dates.dt.strftime(
            "%Y-%m-%d %H:%M:%S" if date_name == "datetime" else "%Y-%m-%d"
        )
        files: dict[str, object] = {}
        for year in sorted(dates.dt.year.unique()):
            selected = source.loc[dates.dt.year.eq(year)].copy()
            filename = f"{code}_{label}_{year}.csv"
            path = staging / filename
            selected.to_csv(path, index=False)
            selected_dates = pd.to_datetime(selected[date_name])
            files[filename] = {
                "frequency": frequency,
                "year": int(year),
                "rows": len(selected),
                "first": selected_dates.min().isoformat(),
                "last": selected_dates.max().isoformat(),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
        return files

    def _publish_market_compatibility(
        self,
        *,
        symbol: str,
        asset: str,
        end_date: str,
        next_session: str,
        staging: Path,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        dataset = Dataset.ETF_OHLCV if asset == "etf" else Dataset.STOCK_OHLCV
        execution_dataset = (
            Dataset.ETF_UNADJUSTED_DAILY
            if asset == "etf"
            else Dataset.STOCK_UNADJUSTED_DAILY
        )
        options = {"env_file": str(Path(self.repo_root) / ".env")}
        adjusted = self._ready(
            self.dataflows.fetch(
                DataRequest(
                    dataset,
                    symbol,
                    self.start_date,
                    end_date,
                    end_date,
                    "daily",
                    options,
                )
            ),
            "adjusted daily publication",
        )
        execution = self._ready(
            self.dataflows.fetch(
                DataRequest(
                    execution_dataset,
                    symbol,
                    self.start_date,
                    end_date,
                    end_date,
                    "daily",
                    options,
                )
            ),
            "execution daily publication",
        )
        code = symbol.split(".", 1)[0]
        adjusted_files = self._write_yearly(
            adjusted.dataframe, staging, code, "daily", "daily"
        )
        execution_files = self._write_yearly(
            execution.dataframe, staging, code, "execution_daily", "daily"
        )
        adjusted_manifest = {
            "schema_version": 3,
            "symbol": symbol,
            "code": code,
            "asset_type": asset,
            "vendor": adjusted.identity.source,
            "requested_start": self.start_date,
            "requested_end": end_date,
            "files": adjusted_files,
            "fetch_metadata": {"daily": dict(adjusted.identity.metadata)},
        }
        execution_manifest = {
            "schema_version": 3,
            "symbol": symbol,
            "code": code,
            "asset_type": asset,
            "vendor": execution.identity.source,
            "adjustment": "none",
            "requested_start": self.start_date,
            "requested_end": end_date,
            "next_trading_session": next_session,
            "files": execution_files,
            "fetch_metadata": dict(execution.identity.metadata),
        }
        validation = {
            "status": "PASS",
            "contract": "dfsl.v1",
            "data_cutoff": end_date,
            "adjusted_identity": adjusted.identity.content_sha256,
            "execution_identity": execution.identity.content_sha256,
        }
        for name, value in (
            (f"{code}_manifest.json", adjusted_manifest),
            (f"{code}_execution_manifest.json", execution_manifest),
            (f"{code}_validation.json", validation),
        ):
            (staging / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        return adjusted_manifest, execution_manifest, validation

    @staticmethod
    def _commit(staging: Path, target: Path) -> list[Path]:
        target.mkdir(parents=True, exist_ok=True)
        backup = staging / "backup"
        backup.mkdir()
        published: list[Path] = []
        moved: list[tuple[Path, Path]] = []
        try:
            sources = sorted(
                (path for path in staging.iterdir() if path.is_file()),
                key=lambda item: (
                    item.name.endswith("_strategy_generation.json"),
                    item.name,
                ),
            )
            # The generation manifest is the publication commit marker.  It
            # must become visible only after every file it authenticates.
            for source in sources:
                destination = target / source.name
                if destination.exists():
                    saved = backup / source.name
                    destination.replace(saved)
                    moved.append((destination, saved))
                source.replace(destination)
                published.append(destination)
        except Exception:
            for path in reversed(published):
                path.unlink(missing_ok=True)
            for destination, saved in moved:
                if saved.exists():
                    saved.replace(destination)
            raise
        return published

    def publish_release(
        self,
        symbol: str,
        asset: str,
        releases: list[tuple[str, str]],
        end_date: str,
    ) -> dict[str, object]:
        """Publish one instrument through SRT and DFLS as one atomic generation."""
        started = time.perf_counter()
        normalized_symbol = symbol.upper()
        selected_releases = sorted(set(releases))
        target = Path(self.data_dir).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        code = normalized_symbol.split(".", 1)[0]
        staging = (target.parent / f".pte-publication-{uuid4().hex}").resolve()
        if staging.parent != target.parent or staging == target:
            raise DataPublicationError("unsafe PTE publication staging path")
        staging.mkdir()
        lock_path = target.parent / f".pte-publication-{code}.lock"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise DataPublicationError(
                f"{normalized_symbol}: another publication is already running"
            ) from exc
        try:
            os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        except Exception:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        os.close(lock_fd)
        try:
            try:
                publications = []
                next_sessions: set[str] = set()
                for strategy_id, strategy_version in selected_releases:
                    declared_symbol = self._declared_symbol(
                        Path(self.repo_root), strategy_id
                    )
                    if declared_symbol != normalized_symbol:
                        raise DataPublicationError(
                            f"{strategy_id}-{strategy_version}: strategy symbol differs from account"
                        )
                    release = self._release(
                        Path(self.repo_root), strategy_id, strategy_version
                    )
                    strategy = StrategyLoader().load(release)
                    deployment = DeploymentSpec(
                        f"publication:{release.release_id}",
                        release.release_id,
                        release.release_hash,
                        normalized_symbol,
                        "publication",
                        "futu_simulate_cn",
                        {
                            "env_file": str(Path(self.repo_root) / ".env"),
                            "repository_root": str(Path(self.repo_root)),
                        },
                    )
                    publication = strategy.publish_data(
                        self.dataflows,
                        deployment,
                        datetime.combine(
                            pd.Timestamp(end_date).date(),
                            datetime_time(20, 30),
                            timezone(timedelta(hours=8), "Asia/Shanghai"),
                        ),
                    )
                    StrategyRunner.validate_publication(strategy, publication)
                    if not publication.ready:
                        raise DataPublicationError(
                            f"{release.release_id}: publication is {publication.status.value}: "
                            f"{publication.error}"
                        )
                    if publication.requested_cutoff != end_date:
                        raise DataPublicationError(
                            f"{release.release_id}: publication cutoff differs from request"
                        )
                    if strategy.definition.execution.settings.get("instrument"):
                        rule_symbol = str(
                            strategy.definition.execution.settings["instrument"]["symbol"]
                        ).upper()
                        if rule_symbol != normalized_symbol:
                            raise DataPublicationError(
                                f"{release.release_id}: strategy symbol differs from account"
                            )
                    calendar_results = [
                        result
                        for name, result in publication.input_results.items()
                        if publication.input_requests[name].dataset
                        == Dataset.TRADING_CALENDAR.value
                    ]
                    for calendar in calendar_results:
                        dates = pd.to_datetime(calendar.dataframe["Date"]).dt.normalize()
                        future = calendar.dataframe.loc[
                            dates.gt(pd.Timestamp(end_date))
                            & calendar.dataframe["IsOpen"].astype(int).eq(1),
                            "Date",
                        ]
                        if not future.empty:
                            next_sessions.add(
                                pd.Timestamp(future.iloc[0]).date().isoformat()
                            )
                    write_publication(publication, staging)
                    publications.append(publication)
                if not next_sessions:
                    raise DataPublicationError("SRT publications have no next trading session")
                if len(next_sessions) != 1:
                    raise DataPublicationError("SRT publications disagree on next trading session")
                next_session = next_sessions.pop()
                self._publish_market_compatibility(
                    symbol=normalized_symbol,
                    asset=asset,
                    end_date=end_date,
                    next_session=next_session,
                    staging=staging,
                )
                release_ids = tuple(item.release_id for item in publications)
                digest = sha256()
                digest.update(normalized_symbol.encode("utf-8"))
                digest.update(end_date.encode("ascii"))
                for path in sorted(
                    (path for path in staging.iterdir() if path.is_file()),
                    key=lambda item: item.name,
                ):
                    digest.update(path.name.encode("utf-8"))
                    digest.update(path.read_bytes())
                generation_id = f"GEN-{digest.hexdigest()[:16].upper()}"
                generation_files = {
                    path.name: sha256(path.read_bytes()).hexdigest()
                    for path in sorted(
                        (path for path in staging.iterdir() if path.is_file()),
                        key=lambda item: item.name,
                    )
                }
                result = {
                    "generation_id": generation_id,
                    "data_cutoff": end_date,
                    "strategy_releases": list(release_ids),
                    "dataset": "runtime",
                    "symbol": normalized_symbol,
                    "asset_type": asset,
                    "publisher": "SRT_DFLS",
                    "files": generation_files,
                }
                (staging / f"{code}_strategy_generation.json").write_text(
                    json.dumps(
                        {"schema_version": 2, **result}, ensure_ascii=False, indent=2
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self._commit(staging, target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                lock_path.unlink(missing_ok=True)
        except Exception as exc:
            if self.audit is not None:
                self.audit.record(
                    "EXTERNAL_CALL_FAILED", source="data_publisher", outcome="FAILURE",
                    actor_type="EXTERNAL", actor_id="dataflows", symbol=normalized_symbol,
                    correlation_id=f"publication:{end_date}",
                    details={
                        "service": "strategy_runtime", "upstream_service": "dataflows",
                        "operation": "publish_data",
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "error_type": type(exc).__name__, "error": str(exc),
                    },
                )
            raise
        if self.audit is not None:
            self.audit.record(
                "EXTERNAL_CALL_SUCCEEDED", source="data_publisher",
                actor_type="EXTERNAL", actor_id="dataflows", symbol=normalized_symbol,
                correlation_id=f"publication:{end_date}",
                details={
                    "service": "strategy_runtime", "upstream_service": "dataflows",
                    "operation": "publish_data",
                    "generation_id": result["generation_id"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
        return result

    def publish(self, end_date: str) -> dict[str, object]:
        accounts = [
            account for account in self.store.strategy_virtual_accounts()
            if account.get("status") != "RETIRED"
        ]
        if not accounts:
            raise DataPublicationError("no active virtual-account instrument to publish")
        releases_by_instrument: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for account in accounts:
            instrument = (str(account["symbol"]).upper(), str(account["asset_type"]))
            releases_by_instrument.setdefault(instrument, []).append(
                (str(account["strategy_id"]), str(account["strategy_version"]))
            )
        published = []
        cutoffs = set()
        for (symbol, asset), releases in sorted(releases_by_instrument.items()):
            result = self.publish_release(symbol, asset, releases, end_date)
            cutoff = result.get("data_cutoff")
            if not isinstance(cutoff, str) or not cutoff:
                raise DataPublicationError(f"{symbol}: publication result missing data_cutoff")
            cutoffs.add(cutoff)
            published.append({"symbol": symbol, "asset_type": asset, "result": result})
        if len(cutoffs) != 1:
            raise DataPublicationError("published instruments have different data cutoffs")
        data_cutoff = cutoffs.pop()
        return {
            "data_cutoff": data_cutoff,
            "instruments": published,
            "generation_ids": [item["result"]["generation_id"] for item in published],
        }
