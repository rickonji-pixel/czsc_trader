# PTE Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以独立 Windows watchdog 服务启动、探活和恢复 PTE CLI 子进程。

**Architecture:** SCM 只托管一个不含交易业务的 watchdog。watchdog 通过 subprocess 执行现有 `pte serve`，用进程状态与本地 HTTP 状态共同判断健康度，并负责退避重启。

**Tech Stack:** Python 3.12、标准库 subprocess/urllib/logging、pywin32、pytest

**Spec:** `docs/superpowers/specs/2026-09-02-pte-watchdog-design.md`

## Global Constraints

- 不改变 PTE 交易、账户、行情和 advice.v2 契约。
- 系统服务名称固定为 `CZSC-PTE-Watchdog`。
- 探活间隔 10 秒，连续 3 次失败触发恢复，退避为 5、30、60 秒。
- HTTP 服务固定监听本机 127.0.0.1:8080。

---

### Task 1: 独立 watchdog 运行循环

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/watchdog.py`
- Create: `packages/paper_trading_engine/tests/test_watchdog.py`

**Interfaces:**
- Consumes: `ServiceConfig.serve_arguments() -> list[str]`
- Produces: `Watchdog.run(stop_event)`, `Watchdog.stop_child()` 和 `http_is_healthy(url, timeout) -> bool`

- [x] **Step 1: Write the failing tests**

覆盖正常启动、连续三次失败后重启、一次失败不重启、停止时回收子进程以及探活响应校验。

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest packages/paper_trading_engine/tests/test_watchdog.py -q`
Expected: FAIL because `paper_trading_engine.watchdog` does not exist.

- [x] **Step 3: Write minimal implementation**

实现可注入 `process_factory`、`health_check` 和 `wait` 的同步运行循环；生产默认值使用 subprocess 与 urllib。

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest packages/paper_trading_engine/tests/test_watchdog.py -q`
Expected: PASS.

### Task 2: Windows SCM 适配与迁移

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/windows_service.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/service_config.py`
- Modify: `packages/paper_trading_engine/tests/test_windows_service.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `Watchdog` and persisted `ServiceConfig`
- Produces: `PteWatchdogService` and `pte-watchdog` console command

- [x] **Step 1: Write failing service tests**

断言新服务名、bootstrap 类、旧服务清理命令、PTE 子进程命令和日志路径。

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest packages/paper_trading_engine/tests/test_windows_service.py -q`
Expected: FAIL on old service identity and direct engine hosting.

- [x] **Step 3: Implement SCM adapter**

服务回调仅构造并运行 Watchdog；安装命令生成 bootstrap、持久化配置、迁移旧服务并设置 SCM 恢复策略。

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest packages/paper_trading_engine/tests/test_windows_service.py -q`
Expected: PASS.

### Task 3: 文档、运行迁移与验收

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-02-paper-trading-operations-design.md`

**Interfaces:**
- Consumes: `pte-watchdog install-config`
- Produces: 可复现安装与状态核验命令

- [x] **Step 1: Update operator documentation**

记录安装、启动、停止、查询、日志位置以及旧服务迁移行为。

- [x] **Step 2: Run full automated verification**

Run: `.venv/Scripts/python.exe -m pytest packages/paper_trading_engine/tests tests -q`
Expected: all tests pass.

- [x] **Step 3: Install and verify the live service**

安装并启动 `CZSC-PTE-Watchdog`，核验服务为 Running/Auto、8080 状态页、暂停/恢复以及 PTE 子进程 PID。

- [x] **Step 4: Commit**

提交 watchdog 实现、测试、文档及执行证据，不合并 master、不推送远端。
