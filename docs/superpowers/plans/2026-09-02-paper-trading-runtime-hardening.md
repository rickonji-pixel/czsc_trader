# Paper Trading Runtime Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PTE run at boot with explicit port, publication, trading-window, intervention, and crash-recovery behavior.

**Architecture:** Keep strategy and pricing behind the `czsc-trader` CLI boundary. Add small runtime-policy and Windows-service modules beside the existing scheduler, engine, and web adapter; persist recovery state in the existing SQLite store and write process logs under ignored runtime state.

**Tech Stack:** Python 3.12, standard-library HTTP server and `zoneinfo`, SQLite, pywin32, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-paper-trading-operations-design.md`

## Global Constraints

- Work on `codex/paper-trading-engine` in the existing checkout; do not create a worktree.
- Keep PTE bound to `127.0.0.1`; default HTTP port is 8080.
- Probe port 8080 before opening Futu or mutating runtime state; never terminate the occupying process.
- Publish complete-close data after 19:00 Asia/Shanghai and retry failures no faster than every five minutes.
- Submit new orders only on `valid_session` during 09:30–11:30 or 13:00–14:57 Asia/Shanghai.
- Pause blocks only new orders; observation and reconciliation continue.
- Preserve intent-before-submit, remark reconciliation, 100-share lots, DAY limits, and broker-side `adjust_limit=0`.
- Installation and removal of `CZSC-PaperTrading` require an elevated terminal; ordinary operation remains least privilege.

## Preflight Snapshot (2026-09-02)

- TCP 8080 is currently available; implementation must probe it again immediately before startup.
- The current shell is not elevated, so service installation cannot be completed from this process.
- `pywin32` is not installed in `.venv`; Task 6 installs it through the package dependency.
- The old foreground listener on 8765 is stopped.

---

### Task 1: Freeze runtime defaults and port preflight

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Test: `packages/paper_trading_engine/tests/test_cli.py`

**Interfaces:**
- Produces: `probe_port(host: str, port: int) -> None` and `serve --port 8080`.
- Consumes: parsed CLI host and port before `build_engine(args)`.

- [ ] **Step 1: Add failing parser and occupied-port tests**

```python
def test_serve_defaults_to_port_8080(tmp_path):
    args = build_parser().parse_args([
        "serve", "--repo-root", str(tmp_path), "--position-size", "50000"
    ])
    assert args.port == 8080

def test_probe_port_reports_conflict(occupied_local_port):
    with pytest.raises(PortUnavailableError, match="already in use"):
        probe_port("127.0.0.1", occupied_local_port)
```

- [ ] **Step 2: Run the focused tests and verify both fail**

Run: `.\.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/test_cli.py -q`

- [ ] **Step 3: Implement preflight before engine construction**

Use an IPv4 TCP socket with `SO_EXCLUSIVEADDRUSE` on Windows, bind to the requested address,
close it immediately, and translate `OSError` into `PortUnavailableError` containing host and port.
Call it before `engine_factory(args)` for `serve`; the HTTP server bind remains the final authority.

- [ ] **Step 4: Run tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/test_cli.py -q`

Commit: `git commit -am "feat: preflight paper trading service port"`

### Task 2: Move complete-close publication to 19:00

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/scheduler.py`
- Test: `packages/paper_trading_engine/tests/test_cli.py`
- Test: `packages/paper_trading_engine/tests/test_scheduler.py`

**Interfaces:**
- Produces: default `--data-refresh-time 19:00` and five-minute failure backoff.
- Consumes: `CliDataPublisher.publish(today: str)` and `PaperStore` settings/events.

- [ ] **Step 1: Add boundary tests for 18:59:59, 19:00:00, failure, and retry**

Assert no call before 19:00, one call at 19:00, no second call at 19:04:59, a retry at
19:05:00, and no further call after `last_data_publish_date` is set.

- [ ] **Step 2: Run focused scheduler tests and verify the default-time assertions fail**

Run: `.\.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/test_scheduler.py packages/paper_trading_engine/tests/test_cli.py -q`

- [ ] **Step 3: Change the defaults and preserve failure visibility**

Keep `DATA_PUBLICATION_FAILED`, `data_publication_error`, `DATA_PUBLISHED`, and successful
error clearing. Do not publish partial data when cross-frequency validation fails.

- [ ] **Step 4: Run tests and commit**

Commit: `git commit -am "feat: publish paper trading data after 19:00"`

### Task 3: Gate submissions to continuous-auction windows

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/trading_window.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Test: `packages/paper_trading_engine/tests/test_trading_window.py`
- Test: `packages/paper_trading_engine/tests/test_engine.py`
- Test: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Produces: `SubmissionClock.now() -> datetime` and `is_submission_window(now: datetime) -> bool`.
- Consumes: timezone-aware Asia/Shanghai time and advice `valid_session`.

- [ ] **Step 1: Add table-driven time-boundary tests**

```python
@pytest.mark.parametrize(("clock", "allowed"), [
    ("09:29:59", False), ("09:30:00", True), ("11:30:00", True),
    ("11:30:01", False), ("13:00:00", True), ("14:57:00", False),
    ("23:59:59", False),
])
def test_submission_window(clock, allowed): ...
```

- [ ] **Step 2: Add an engine regression test for an overnight decision**

Create a BUY decision valid today, set the clock to 00:00, and assert no broker submission plus
`OUTSIDE_SUBMISSION_WINDOW`; advance to 09:30 and assert exactly one submission.

- [ ] **Step 3: Implement the market-session gate before `_submit_once`**

Evaluate conditions in this order: paused, valid session, submission window, active order,
existing intent. Continue order/account polling outside the window. Add the Chinese presentation
label `OUTSIDE_SUBMISSION_WINDOW: 当前不在自动发单时段`.

- [ ] **Step 4: Run focused tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/test_trading_window.py packages/paper_trading_engine/tests/test_engine.py packages/paper_trading_engine/tests/test_web.py -q`

Commit after explicitly adding `trading_window.py`, its tests, and the modified engine/web files:
`git commit -m "feat: gate paper orders to trading sessions"`

### Task 4: Recover uncertain submissions after crashes

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Test: `packages/paper_trading_engine/tests/test_engine.py`

**Interfaces:**
- Produces: `PaperStore.pending_intents()` and explicit intent states `PENDING_SUBMIT`, `SUBMITTED`, `SUBMIT_FAILED`.
- Consumes: broker orders keyed by PTE remark.

- [ ] **Step 1: Add crash-boundary tests**

Cover: intent persisted with no response; restart finds a broker order by remark and binds it;
restart finds no broker order and retries only inside the valid trading window; a bound or terminal
intent never creates a duplicate order.

- [ ] **Step 2: Run the focused tests and verify recovery cases fail**

Run: `.\.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/test_engine.py -q`

- [ ] **Step 3: Implement remark-first recovery**

Reconcile all broker orders before retrying. Reuse the same `intent_id`; record each retry and
failure as an event. Never create a second intent for the same `decision_id`.

- [ ] **Step 4: Run tests and commit**

Commit: `git commit -am "fix: recover uncertain paper order submissions"`

### Task 5: Replace Pause and Resume with one run switch

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Test: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Produces: one accessible switch backed by `/api/pause` and `/api/resume`.
- Consumes: server-confirmed `paused: bool`.

- [ ] **Step 1: Add HTML and interaction regression assertions**

Assert one checkbox/button with `role="switch"`, `aria-checked`, Chinese current-state text,
no separate pause/resume buttons, correct endpoint selection, immediate success/failure feedback,
and rollback to the server-confirmed state after failure.

- [ ] **Step 2: Run the web tests and verify they fail**

- [ ] **Step 3: Implement one stateful switch without optimistic state drift**

Disable it only while the request is in flight. Render the returned status on success; on failure,
show the error and refresh `/api/status` before enabling it again.

- [ ] **Step 4: Run tests and commit**

Commit: `git commit -am "fix: use one paper trading run switch"`

### Task 6: Add Windows service lifecycle and logs

**Files:**
- Modify: `packages/paper_trading_engine/pyproject.toml`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/windows_service.py`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/service_config.py`
- Create: `packages/paper_trading_engine/tests/test_service_config.py`
- Create: `packages/paper_trading_engine/tests/test_windows_service.py`
- Modify: `packages/paper_trading_engine/README.md`

**Interfaces:**
- Produces: `pte-service install|start|stop|restart|status|remove` for `CZSC-PaperTrading`.
- Consumes: persisted JSON config, `cli.main(["serve", ...])`, Windows SCM, and the existing SQLite state.

- [ ] **Step 1: Add `pywin32>=311` as a Windows-only dependency and add `pte-service` entry point**

Use `pywin32>=311; platform_system == 'Windows'`. Keep importing it inside the Windows-service
module so normal package imports remain portable.

- [ ] **Step 2: Add config validation tests**

The service config requires absolute `repo_root`, position/allocation mode, localhost host, port,
OpenD host/port, and log path. Reject secrets, relative paths, non-local HTTP binding, and unknown keys.

- [ ] **Step 3: Add mocked SCM lifecycle tests**

Verify automatic start, service name, config registry path, restart actions after 5/30/60 seconds,
and graceful stop signaling. Installation without elevation must return a direct remediation message.

- [ ] **Step 4: Implement the pywin32 service host**

Run the scheduler and HTTP server in the service process, write rotating UTF-8 logs under
`state/paper_trading/logs`, preserve the database across restarts, and stop both loops before
closing Futu contexts and SQLite.

- [ ] **Step 5: Run unit tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/test_service_config.py packages/paper_trading_engine/tests/test_windows_service.py -q`

Commit after explicitly adding both new service modules, both tests, `pyproject.toml`, and README:
`git commit -m "feat: run paper trading as a Windows service"`

### Task 7: Complete runtime integration verification

**Files:**
- Modify when verified: `packages/paper_trading_engine/README.md`
- Modify when verified: `docs/superpowers/specs/2026-09-02-paper-trading-operations-design.md`

**Interfaces:**
- Consumes: all runtime-hardening tasks.
- Produces: verified service installation/runbook; does not merge or push.

- [ ] **Step 1: Run all automated checks**

Run PTE tests, root tests, Ruff, and `git diff --check` using the existing project commands.

- [ ] **Step 2: Probe port 8080 and perform a foreground smoke test**

Confirm `/`, `/api/status`, the run switch, Chinese labels, 19:00 setting, and zero submissions
outside the trading window.

- [ ] **Step 3: Install the service from an elevated terminal**

Record the config path, start type, service account, recovery policy, and log path. If elevation is
unavailable, stop at a generated administrator command; do not claim installation succeeded.

- [ ] **Step 4: Verify crash recovery and clean shutdown**

Terminate the service process once through the SCM test procedure, verify automatic restart and
unchanged SQLite state, then stop it normally and verify no orphan listener remains.

- [ ] **Step 5: Commit verification documentation**

Commit: `git commit -am "docs: verify paper trading runtime service"`
