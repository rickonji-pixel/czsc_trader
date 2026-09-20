"""Atomic production-data publication owned by SRT."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import re
import shutil
from uuid import uuid4

import pandas as pd
from dataflows import DataRequest, DataStatus, Dataflows, Dataset
from .loader import StrategyLoader
from .models import DeploymentSpec, StrategyRelease
from .publication_store import write_publication
from .runner import StrategyRunner


class StrategyPublicationError(RuntimeError):
    pass


class StrategyDataPublisher:
    """Publish explicit frozen releases without depending on an execution host."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config_root: Path | None = None,
        data_dir: Path,
        dataflows: Dataflows | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config_root = Path(config_root) if config_root is not None else Path(repo_root)
        self.data_dir = data_dir
        self.dataflows = dataflows or Dataflows()

    def publication_target(self, as_of: date) -> str:
        """Latest mainland session, using the vendor calendar including closed days."""
        start = as_of - timedelta(days=31)
        result = self._ready(self.dataflows.fetch(DataRequest(
            Dataset.TRADING_CALENDAR, "SSE", start.isoformat(), as_of.isoformat(),
            as_of.isoformat(), options={"env_file": str(self.config_root / ".env")},
        )), "publication trading calendar")
        frame = result.dataframe
        dates = pd.DatetimeIndex(pd.to_datetime(frame["Date"], errors="raise"))
        expected = pd.date_range(start, as_of, freq="D")
        if not dates.equals(expected) or not frame["IsOpen"].isin([0, 1]).all():
            raise StrategyPublicationError("publication trading calendar is incomplete or invalid")
        sessions = dates[frame["IsOpen"].eq(1).to_numpy()]
        if sessions.empty:
            raise StrategyPublicationError("publication trading calendar has no open session")
        return sessions[-1].date().isoformat()

    @staticmethod
    def _release(repo_root: Path, strategy_id: str, version: str) -> StrategyRelease:
        path = Path(repo_root) / "strategies" / strategy_id / "versions" / f"{version}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StrategyPublicationError(
                f"cannot load frozen strategy {strategy_id}-{version}: {exc}"
            ) from exc
        return StrategyRelease.from_mapping(payload)

    @staticmethod
    def _declared_symbol(repo_root: Path, strategy_id: str) -> str:
        path = Path(repo_root) / "strategies" / strategy_id / "family.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            scope = payload["scope"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StrategyPublicationError(
                f"cannot read strategy scope for {strategy_id}: {exc}"
            ) from exc
        if not isinstance(scope, list) or len(scope) != 1 or not isinstance(scope[0], str):
            raise StrategyPublicationError(
                f"{strategy_id}: runtime publication requires one symbol"
            )
        return scope[0].upper()

    @staticmethod
    def _ready(result, label: str):
        if result.status is not DataStatus.READY:
            detail = result.error.message if result.error else result.status.value
            raise StrategyPublicationError(f"{label}: {result.status.value}: {detail}")
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
        publications,
        symbol: str,
        asset: str,
        end_date: str,
        next_session: str,
        staging: Path,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        adjusted_dataset = (
            Dataset.ETF_OHLCV.value if asset == "etf" else Dataset.STOCK_OHLCV.value
        )
        execution_dataset = (
            Dataset.ETF_UNADJUSTED_DAILY.value
            if asset == "etf"
            else Dataset.STOCK_UNADJUSTED_DAILY.value
        )
        adjusted: dict[str, object] = {}
        execution = None
        for publication in publications:
            for name, request in publication.input_requests.items():
                result = publication.input_results[name]
                if request.dataset == Dataset.TRADING_CALENDAR.value:
                    continue
                subject = None if request.symbol is None else request.symbol.upper()
                if subject != symbol:
                    continue
                if request.dataset == adjusted_dataset:
                    current = adjusted.get(request.frequency)
                    if current is None:
                        adjusted[request.frequency] = result
                    else:
                        old_start = pd.to_datetime(current.dataframe["Date"]).min()
                        new_start = pd.to_datetime(result.dataframe["Date"]).min()
                        if new_start < old_start:
                            adjusted[request.frequency] = result
                elif request.dataset == execution_dataset and request.frequency == "daily":
                    if execution is None:
                        execution = result
        if "daily" not in adjusted:
            raise StrategyPublicationError("SRT publications have no adjusted daily input")
        if execution is None:
            raise StrategyPublicationError("SRT publications have no execution daily input")
        code = symbol.split(".", 1)[0]
        adjusted_files: dict[str, object] = {}
        for frequency, result in sorted(adjusted.items()):
            adjusted_files.update(
                self._write_yearly(result.dataframe, staging, code, frequency, frequency)
            )
        execution_files = self._write_yearly(
            execution.dataframe, staging, code, "execution_daily", "daily"
        )
        adjusted_starts = [
            pd.to_datetime(result.dataframe["Date"]).min() for result in adjusted.values()
        ]
        adjusted_identities = {
            frequency: result.identity.content_sha256
            for frequency, result in sorted(adjusted.items())
        }
        adjusted_manifest = {
            "schema_version": 3,
            "symbol": symbol,
            "code": code,
            "asset_type": asset,
            "vendor": adjusted["daily"].identity.source,
            "requested_start": min(adjusted_starts).date().isoformat(),
            "requested_end": end_date,
            "files": adjusted_files,
            "fetch_metadata": {
                frequency: dict(result.identity.metadata)
                for frequency, result in sorted(adjusted.items())
            },
        }
        execution_manifest = {
            "schema_version": 3,
            "symbol": symbol,
            "code": code,
            "asset_type": asset,
            "vendor": execution.identity.source,
            "adjustment": "none",
            "requested_start": pd.to_datetime(execution.dataframe["Date"])
            .min()
            .date()
            .isoformat(),
            "requested_end": end_date,
            "next_trading_session": next_session,
            "files": execution_files,
            "fetch_metadata": dict(execution.identity.metadata),
        }
        validation = {
            "status": "PASS",
            "contract": "srt.market-compatibility.v1",
            "data_cutoff": end_date,
            "adjusted_identities": adjusted_identities,
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
        normalized_symbol = symbol.upper()
        if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", normalized_symbol):
            raise StrategyPublicationError("publication symbol must be an A-share instrument")
        if asset not in {"etf", "stock"}:
            raise StrategyPublicationError("publication asset must be etf or stock")
        selected_releases = sorted(set(releases))
        if not selected_releases:
            raise StrategyPublicationError("at least one frozen strategy release is required")
        if any(
            not re.fullmatch(r"S\d{3,}", strategy_id)
            or not re.fullmatch(r"v\d+", version)
            for strategy_id, version in selected_releases
        ):
            raise StrategyPublicationError("strategy release identity is unsafe")
        target = Path(self.data_dir).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        code = normalized_symbol.split(".", 1)[0]
        staging = (target.parent / f".srt-publication-{uuid4().hex}").resolve()
        if staging.parent != target.parent or staging == target:
            raise StrategyPublicationError("unsafe SRT publication staging path")
        staging.mkdir()
        lock_path = target.parent / f".srt-publication-{code}.lock"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise StrategyPublicationError(
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
            publications = []
            next_sessions: set[str] = set()
            for strategy_id, strategy_version in selected_releases:
                declared_symbol = self._declared_symbol(
                    Path(self.repo_root), strategy_id
                )
                if declared_symbol != normalized_symbol:
                    raise StrategyPublicationError(
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
                        "env_file": str(self.config_root / ".env"),
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
                    raise StrategyPublicationError(
                        f"{release.release_id}: publication is {publication.status.value}: "
                        f"{publication.error}"
                    )
                if publication.requested_cutoff != end_date:
                    raise StrategyPublicationError(
                        f"{release.release_id}: publication cutoff differs from request"
                    )
                if strategy.definition.execution.settings.get("instrument"):
                    rule_symbol = str(
                        strategy.definition.execution.settings["instrument"]["symbol"]
                    ).upper()
                    if rule_symbol != normalized_symbol:
                        raise StrategyPublicationError(
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
                raise StrategyPublicationError(
                    "SRT publications have no next trading session"
                )
            if len(next_sessions) != 1:
                raise StrategyPublicationError(
                    "SRT publications disagree on next trading session"
                )
            next_session = next_sessions.pop()
            self._publish_market_compatibility(
                publications=publications,
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
        return result
