# A股股票与 ETF 通用固定基线回测 Implementation Plan

**Execution status:** Implementation completed on `codex/generic-market-backtest`; final verification evidence is recorded in the delivery response.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立经过验证的 A股股票/ETF 数据准备工具、不可变规则基线注册表，以及默认使用最新基线的任意标的固定规则回测入口。

**Architecture:** 数据准备命令通过 `dataflows` 的公开 DataFrame API 获取三周期行情，在临时目录完成验证后发布到现有扁平 `data/raw`。通用加载器只接受清单和哈希均通过的数据；独立回测编排器解析不可变规则基线、应用固定规则、执行 vectorbt 与独立账本回测并输出审计产物，绝不调用候选搜索。

**Tech Stack:** Python 3.12、pandas、Tushare、CZSC 1.0.1、vectorbt 1.1.0、Plotly 6.9.0、pytest 8+

**Spec:** `docs/superpowers/specs/2026-08-23-generic-market-backtest-design.md`

## Global Constraints

- 仅支持 `.SH`、`.SZ` 的 A股股票和 ETF；资产类型必须明确为 `stock` 或 `etf`。
- 行情保持 `data/raw/<六位代码>_<30m|daily|weekly>_<年份>.csv` 扁平布局和 `utf-8-sig` 编码。
- 回测不联网、不改写行情、不遍历候选规则、不更新规则基线。
- `baseline_v001` 必须与 `outputs/588080_0823_R06/selected_rule.json` 内容一致且不可原地修改。
- 默认基线由注册表 `latest` 指定；每次回测固化实际版本、规则和 SHA-256。
- 缺省验收目标时状态为 `N/A`；只有传入目标配置时才判定 PASS/FAIL。
- 输出目录固定为 `outputs/<六位代码>_<MMDD>_RXX`，使用 Asia/Shanghai 日期且不覆盖。
- 交易信号 T 日收盘形成，最早 T+1 开盘执行，只做多/空仓，单边费率默认 `0.0005`，初始资金默认 `1_000_000`。
- 默认测试排除 `slow` 和 `network`；Tushare token 不能出现在输出、日志或 Git 中。
- 遵循项目 `AGENTS.md`：使用普通 Git 分支、当前主会话，不使用 worktree 或子 agent。

---

### Task 1: 不可变规则基线注册表

**Files:**
- Create: `src/czsc_trader/baselines.py`
- Create: `configs/rule_baselines/baseline_v001.json`
- Create: `configs/rule_baselines/registry.json`
- Create: `tests/test_baselines.py`

**Interfaces:**
- Produces: `ResolvedBaseline(version: str, rule: Rule, rule_payload: dict[str, object], sha256: str, source_output: str)`
- Produces: `resolve_baseline(root: Path, version: str | None = None) -> ResolvedBaseline`
- Produces: `promote_baseline(root: Path, selected_rule: Path, source_output: str, now: datetime) -> ResolvedBaseline`

- [ ] **Step 1: Write failing baseline resolution tests**

```python
def test_resolve_latest_baseline_verifies_hash(tmp_path: Path) -> None:
    root = write_registry_fixture(tmp_path, latest="baseline_v001")
    resolved = resolve_baseline(root)
    assert resolved.version == "baseline_v001"
    assert resolved.rule.weights == (0.3, 0.3, 0.4)

def test_resolve_baseline_rejects_modified_rule(tmp_path: Path) -> None:
    root = write_registry_fixture(tmp_path, latest="baseline_v001")
    (root / "baseline_v001.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        resolve_baseline(root)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_baselines.py -q`

Expected: collection fails because `czsc_trader.baselines` does not exist.

- [ ] **Step 3: Implement strict resolution and Rule validation**

Implement canonical JSON reading, byte-level SHA-256 comparison, version lookup, and construction of the existing `walk_forward.Rule`. Reject missing versions, files, fields, invalid values, and hash mismatches with specific `ValueError` messages.

- [ ] **Step 4: Add failing promotion tests**

```python
def test_promote_adds_next_immutable_version_and_updates_latest(tmp_path: Path) -> None:
    root = write_registry_fixture(tmp_path, latest="baseline_v001")
    selected = tmp_path / "selected_rule.json"
    selected.write_text(json.dumps(VALID_RULE), encoding="utf-8")
    promoted = promote_baseline(root, selected, "outputs/example_R02", FIXED_NOW)
    assert promoted.version == "baseline_v002"
    assert json.loads((root / "registry.json").read_text(encoding="utf-8"))["latest"] == "baseline_v002"
```

- [ ] **Step 5: Implement explicit promotion with atomic registry replacement**

Copy the exact rule payload into a newly numbered file, refuse any existing target filename, write the registry through a `.tmp` file, then resolve the new version again before returning it. Do not connect this function to research execution.

- [ ] **Step 6: Freeze R06 as baseline_v001 and verify byte-equivalent JSON content**

Copy the rule object from `outputs/588080_0823_R06/selected_rule.json`, calculate its actual byte SHA-256 for `registry.json`, and record `source_output` as `outputs/588080_0823_R06`.

- [ ] **Step 7: Run baseline tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_baselines.py -q`

Expected: all tests pass.

Commit: `feat: freeze versioned rule baseline`

---

### Task 2: 公开 Tushare DataFrame 获取接口

**Files:**
- Modify: `dataflows/tushare_stock.py`
- Modify: `dataflows/tushare_etf.py`
- Create: `tests/test_dataflow_fetch.py`

**Interfaces:**
- Produces: `fetch_stock_ohlcv(symbol: str, start_date: str, end_date: str, period: str) -> tuple[pd.DataFrame, dict[str, str]]`
- Produces: `fetch_etf_ohlcv(symbol: str, start_date: str, end_date: str, period: str) -> tuple[pd.DataFrame, dict[str, str]]`
- DataFrame columns remain `Date,Open,High,Low,Close,Volume,Amount`.

- [ ] **Step 1: Write failing public-interface tests**

Patch the internal vendor boundary to return a deterministic normalized DataFrame and assert that the public function returns the frame plus metadata containing `vendor=tushare`, `market=A-share`, the resolved vendor symbol, period, and asset type. Add a test that vendor exceptions propagate rather than being converted into an `Error retrieving...` string.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_dataflow_fetch.py -q`

Expected: import fails because the public functions do not exist.

- [ ] **Step 3: Implement thin public DataFrame APIs**

Reuse `_fetch_tushare_ohlcv` and `_fetch_tushare_etf_ohlcv`, reject empty results, return a copied DataFrame and metadata, and keep existing human-readable `get_stock`/`get_etf` behavior compatible.

- [ ] **Step 4: Run focused tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_dataflow_fetch.py -q`

Expected: all tests pass.

Commit: `feat: expose Tushare dataframe fetch APIs`

---

### Task 3: 行情规范化、验证和安全发布

**Files:**
- Create: `src/czsc_trader/market_data_prep.py`
- Create: `scripts/prepare_market_data.py`
- Create: `tests/test_market_data_prep.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `prepare_market_data(symbol: str, asset_type: str, start: date, end: date, data_dir: Path, fetcher: MarketFetcher | None = None) -> dict[str, object]`
- Produces: `validate_market_frames(intraday: pd.DataFrame, daily: pd.DataFrame, weekly: pd.DataFrame) -> dict[str, object]`
- Produces: CLI exit code 0 only after manifest and validation status PASS are published.

- [ ] **Step 1: Write failing validation tests**

Build two complete trading days with the eight approved 30-minute close times. Assert valid frames return matched-day counts. Add isolated tests for duplicate timestamps, seven-bar days, invalid OHLC, 30m/daily mismatch, and daily/weekly mismatch.

- [ ] **Step 2: Run validation tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_market_data_prep.py -q`

Expected: import fails because `market_data_prep` does not exist.

- [ ] **Step 3: Implement normalization and validation**

Convert `dataflows` title-case columns to project CSV fields, validate exact A-share sessions, aggregate 30m to daily and daily to `W-SUN`, use named price/volume/amount tolerances, and return a serializable validation payload.

- [ ] **Step 4: Write failing publishing tests**

```python
def test_prepare_splits_years_and_writes_pass_manifest(tmp_path: Path) -> None:
    result = prepare_market_data("600519.SH", "stock", START, END, tmp_path, fetcher=fake_fetcher)
    assert result["validation_status"] == "PASS"
    assert (tmp_path / "600519_30m_2026.csv").is_file()
    manifest = json.loads((tmp_path / "600519_manifest.json").read_text(encoding="utf-8"))
    assert manifest["symbol"] == "600519.SH"
    assert manifest["files"]["600519_30m_2026.csv"]["sha256"]

def test_failed_validation_does_not_replace_existing_files(tmp_path: Path) -> None:
    existing = tmp_path / "600519_daily_2026.csv"
    existing.write_bytes(b"old")
    with pytest.raises(ValueError, match="reconciliation"):
        prepare_market_data("600519.SH", "stock", START, END, tmp_path, fetcher=bad_fetcher)
    assert existing.read_bytes() == b"old"
```

- [ ] **Step 5: Implement staged split, hashes, manifest, validation report, rollback and CLI**

Fetch `30m`, `daily`, and `weekly`; write all candidate files into a temporary sibling directory; validate there; back up files that will be replaced; publish using `Path.replace`; restore backups on any exception. Never serialize token or environment variables. Parse dates and explicit asset type in `scripts/prepare_market_data.py`, print JSON summary, and return nonzero on failure.

- [ ] **Step 6: Register test markers**

Add `slow` and `network` markers to `[tool.pytest.ini_options]` and change default `addopts` to `--strict-markers --tb=short -m "not slow and not network"`.

- [ ] **Step 7: Run preparation tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_market_data_prep.py tests/test_dataflow_fetch.py -q`

Expected: all tests pass with no network access.

Commit: `feat: prepare and validate flat market data`

---

### Task 4: 通用清单驱动数据加载器

**Files:**
- Modify: `src/czsc_trader/data.py`
- Modify: `tests/test_data.py`
- Create: `data/raw/588080_manifest.json`
- Create: `data/raw/588080_validation.json`

**Interfaces:**
- Changes: `load_market_data(raw_dir: Path, symbol: str = "588080.SH", asset_type: str | None = None) -> MarketData`
- Extends: `MarketData` with `symbol`, `asset_type`, `manifest`, and existing `hashes`.
- Keeps: `MarketData.truncate()` preserves metadata.

- [ ] **Step 1: Write failing generic loader tests**

Use a temporary manifest listing non-fixed years and assert `load_market_data(tmp_path, "600519.SH", "stock")` discovers only listed files and inserts the requested symbol. Add tests that reject validation status other than PASS, file hash mismatch, manifest symbol mismatch, and asset mismatch.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data.py -q`

Expected: failures show the loader still hardcodes `588080`, years, and README hashes.

- [ ] **Step 3: Implement manifest-driven generic loading**

Remove fixed `_required_paths` and README parsing from the primary path. Read files in manifest order, infer frequency from each listed filename, verify hashes, normalize columns, reconcile frames, and preserve `symbol`/`asset_type` metadata. Keep the default symbol only to preserve existing research calls.

- [ ] **Step 4: Generate and verify 588080 compatibility metadata**

Calculate hashes from the tracked raw CSV files, write a PASS validation report, set asset type `etf`, and list the actual 2024—2026 files. Verify the default `load_market_data(Path("data/raw"))` still loads all three frequencies.

- [ ] **Step 5: Run data and research compatibility tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data.py tests/test_factors.py tests/test_research.py -q`

Expected: all selected tests pass.

Commit: `refactor: load market data by symbol manifest`

---

### Task 5: 从候选研究中提取固定规则应用

**Files:**
- Modify: `src/czsc_trader/walk_forward.py`
- Modify: `tests/test_walk_forward.py`

**Interfaces:**
- Produces: `AppliedRule(target_position: pd.Series, scores: pd.Series, events: pd.DataFrame)`
- Produces: `apply_fixed_rule(factors: pd.DataFrame, rule: Rule) -> AppliedRule`
- Existing `select_fixed_rule` reuses this function for the selected/candidate target generation without changing ranking semantics.

- [ ] **Step 1: Write failing fixed-application test**

Construct a small factor frame and a known `Rule`; assert exact target positions, score values and Entry/Exit event dates from `apply_fixed_rule`. Add a test that monkeypatches `CANDIDATES` to raise if a fixed-rule call touches candidate iteration.

- [ ] **Step 2: Run test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_walk_forward.py -k "apply_fixed" -q`

Expected: import or attribute failure because `apply_fixed_rule` does not exist.

- [ ] **Step 3: Extract minimal fixed-rule function**

Reuse the existing score and state-machine behavior exactly. Do not change candidate definitions, ranking keys, target periods, or selected R06 behavior.

- [ ] **Step 4: Run focused and existing state-machine tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_walk_forward.py -q`

Expected: all non-slow walk-forward tests pass.

Commit: `refactor: expose fixed rule application`

---

### Task 6: 可选目标语义与固定规则回测编排

**Files:**
- Modify: `src/czsc_trader/backtest.py`
- Create: `src/czsc_trader/backtest_runner.py`
- Create: `scripts/run_backtest.py`
- Create: `tests/test_backtest_runner.py`
- Modify: `tests/test_backtest.py`

**Interfaces:**
- Changes: `run_period_backtests(..., return_targets: dict[str, float] | None = None)` treats `None` as no acceptance evaluation rather than importing research defaults.
- Produces: `BacktestRequest(symbol, asset_type, start, end, baseline, targets_path, fee_rate, init_cash, raw_dir, outputs_root)`.
- Produces: `run_fixed_backtest(request: BacktestRequest, run_date: date | None = None) -> dict[str, object]`.

- [ ] **Step 1: Write failing optional-target tests**

Assert a target-free period result has `status == "N/A"` and no implicit 588080 target, while explicitly supplied thresholds produce inclusive PASS/FAIL. Preserve independent cash and prior-signal execution behavior.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_backtest.py -q`

Expected: target-free call currently imports `RETURN_TARGETS` or rejects period names.

- [ ] **Step 3: Implement explicit optional acceptance evaluation**

When `return_targets is None`, allow arbitrary period names and write `target_return=None`, `target_margin=None`, `pass=None`, `status="N/A"`. When provided, require exact period-name equality and keep inclusive absolute target evaluation.

- [ ] **Step 4: Write failing end-to-end runner tests**

Use small validated local fixtures and a test registry. Assert the runner resolves latest baseline, calls `apply_fixed_rule`, creates `<code>_<MMDD>_R01`, writes actual baseline/data hashes, emits required CSV/JSON/report/chart files, and does not emit `candidate_results.csv` or `selected_rule.json`. Add tests for explicit historical baseline, date slicing, target-config/date argument conflict, asset mismatch and missing prior signal.

- [ ] **Step 5: Run runner tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_backtest_runner.py -q`

Expected: import fails because `backtest_runner` and CLI do not exist.

- [ ] **Step 6: Implement fixed-rule orchestration and CLI**

Load validated data, resolve baseline, generate factors, apply the rule once over causal history, choose one `full` period or parse named target periods, run period backtests, build factor provenance, audit orders, render shared-hover charts, and atomically write all specified artifacts. Copy the resolved payload to `baseline_rule.json`; write `failure.json` on a post-directory failure and return nonzero from the CLI.

- [ ] **Step 7: Run runner/backtest/chart tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_backtest.py tests/test_backtest_runner.py tests/test_charting.py tests/test_audit.py tests/test_output_paths.py -q`

Expected: all selected tests pass.

Commit: `feat: add generic fixed baseline backtest`

---

### Task 7: 文档、CLI 冒烟与交付验证

**Files:**
- Modify: `README.md`
- Modify: `dataflows/README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `docs/superpowers/plans/2026-08-23-generic-market-backtest.md`

**Interfaces:**
- Documents exact stock/ETF preparation commands, default/latest and explicit baseline backtests, target-free N/A semantics, slow/network test commands, and the research/backtest distinction.

- [ ] **Step 1: Update operational documentation**

Document this sequence:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_market_data.py --symbol 600519.SH --asset stock --start 2024-01-01 --end 2026-08-21
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol 600519.SH --asset stock
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol 600519.SH --asset stock --baseline baseline_v001
```

State clearly that `run_research.py` searches/selects rules, while `run_backtest.py` applies one frozen rule and never updates it.

- [ ] **Step 2: Run syntax and CLI help checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src dataflows scripts
.\.venv\Scripts\python.exe scripts\prepare_market_data.py --help
.\.venv\Scripts\python.exe scripts\run_backtest.py --help
```

Expected: exit code 0 for all commands.

- [ ] **Step 3: Run the fast suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: all unmarked tests pass; `slow` and `network` are deselected.

- [ ] **Step 4: Run a local 588080 fixed-baseline smoke backtest**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol 588080.SH --asset etf --start 2026-01-01 --end 2026-08-21 --baseline baseline_v001
```

Expected: a new `outputs/588080_0823_RXX` directory containing manifest, baseline rule, metrics, orders, equity, factors, events, audit, chart and report; no candidate results; audit status PASS; acceptance status N/A.

- [ ] **Step 5: Inspect generated evidence and Git diff**

Verify output directory naming, manifest data/baseline hashes, N/A status, no candidate artifacts, and no token. Run `git diff --check` and `git status --short`.

- [ ] **Step 6: Commit documentation and plan completion marks**

Commit: `docs: document generic baseline backtesting`

- [ ] **Step 7: Apply verification-before-completion**

Re-run the fast suite and the focused smoke command from fresh state, read exit codes and final counts, then report exact test evidence, smoke output path, baseline version/hash, data range, return metrics, and any intentionally unrun `slow` or `network` checks.
