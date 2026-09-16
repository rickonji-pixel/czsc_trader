# macOS 安装与启动指南

> 本文是[用户使用说明](USER_GUIDE.md)的 macOS（zsh/bash）命令对照版，覆盖在 macOS 上
> 安装、离线验证、研究回测、以及 PTE 前台运行模拟交易的完整流程。业务语义、账户规则、
> 调度节奏和安全约束与 Windows 版完全一致；平台差异集中在包路径、日志和进程托管方式。
> 以下命令均在仓库根目录执行，目录下应能看到 `pyproject.toml`、`src/` 和 `packages/`。

## 1. 平台支持范围

| 能力 | macOS 是否支持 | 说明 |
| --- | --- | --- |
| TDR：数据校验、回测、决策、研究编排 | 是 | 与 Windows 相同 |
| 六模块离线功能测试、Ruff | 是 | 不连 Tushare/Futu，适合开发与学习 |
| PTE：`once` / `serve` 前台进程、账户管理、绩效导出 | 是 | 模拟下单需要本机运行 Futu OpenD for Mac |
| PTE Web 控制台 | 是 | 默认 <http://127.0.0.1:8080>，强制只监听 localhost |
| WDG 开机自启/故障拉起 | 否 | `pte-watchdog` 是 Windows 服务（依赖 pywin32），macOS 没有等价托管层，需要自己保持终端或后台进程 |
| Futu 模拟交易渠道 | 取决于外部程序 | 需另行安装启动 Futu OpenD for Mac，并登录中国市场模拟账户 |

`pywin32` 带 Windows 环境标记（`platform_system == 'Windows'`），在 macOS 上 pip 会自动跳过；
但 `.venv/bin/` 下仍会生成 `pte-watchdog` 入口，调用会因缺少 pywin32 失败，**不要在 macOS 上使用它**。

## 2. 前置条件

### 2.1 Python 3.12（必须，不要用 3.14）

项目要求 Python 3.12（[根 pyproject](../pyproject.toml) 与各子包均为 `requires-python >= 3.12`）。
注意 `vectorbt`、`tsfresh` 等重型依赖在 3.14 上可能没有预编译 wheel，因此**必须用 3.12 建虚拟环境**，
不要直接用系统/conda 默认的 `python3`。

先确认解释器位置和版本：

```bash
which -a python3.12 python3
python3.12 --version          # 期望输出 Python 3.12.x
python3 --version             # 若显示 3.13/3.14，只用于对照，建 venv 时不要用它
file "$(which python3.12)"    # Apple Silicon 期望路径架构含 arm64
```

没有 3.12 时，通过 Homebrew 安装（或从 python.org 安装 universal2 安装包）：

```bash
brew install python@3.12
```

### 2.2 其他前置（按需）

- **Node.js 18+**：仅在需要运行 PTE 前端 `console_state.test.mjs` 时需要；不跑该测试可不装。
- **Tushare Token**：只有发布/更新行情（`data prepare`、`data update-backtest`、每日数据发布）
  时需要。离线测试和使用仓库已发布数据不需要。复制模板并填写：

  ```bash
  cp .env.example .env
  # 编辑 .env：TUSHARE_TOKEN=你的token
  ```

- **Futu OpenD for Mac**：只有让 PTE 连接模拟渠道实际下单/对账时需要，默认连接
  `127.0.0.1:11111`（可用 `--opend-host/--opend-port` 覆盖）。OpenD 未启动时 PTE 仍能启动，
  但渠道进入降级状态并记录 `DEPENDENCY_DEGRADED` 审计事件，不能提交订单。
- **新闻 MaaS 凭据**：只在使用 `czsc-trader news extract` 时需要，可选写入 `.env`：

  ```dotenv
  CZSC_NEWS_MAAS_API_KEY=<API Key>
  CZSC_NEWS_MAAS_MODEL=<控制台显示的 ep-* 服务ID>
  # 可选：CZSC_NEWS_MAAS_API_URL=<非默认地域地址>
  ```

## 2.3 处理已有的错误版本 .venv

如果 `.venv/` 已经存在但不是 3.12（例如用 conda 的 python 3.14 创建），需要删除重建。
先确认，再自行决定是否删除（会移除所有已装依赖）：

```bash
.venv/bin/python --version
# 若不是 Python 3.12.x：
rm -rf .venv
```

## 3. 安装

在仓库根目录按固定顺序做 7 个 editable 安装（PTE 放最后，依赖前六个包）：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip

.venv/bin/python -m pip install -e packages/dataflows
.venv/bin/python -m pip install -e "packages/factor_signal_catalog[test]"
.venv/bin/python -m pip install -e "packages/strategy_template_catalog[test]"
.venv/bin/python -m pip install -e "packages/strategy_manager[test]"
.venv/bin/python -m pip install -e "packages/strategy_evaluator[test]"
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pip install -e "packages/paper_trading_engine[test]"
```

安装后确认两个入口和 venv 版本：

```bash
.venv/bin/python --version
.venv/bin/czsc-trader --help
.venv/bin/pte --help
```

## 4. 安装后的离线验证

离线测试使用固定小数据和 `.tmp/` 临时目录，不连 Tushare、Futu，也不修改正式 `state/`。
先跑最小的一个模块确认环境：

```bash
.venv/bin/python -m pytest -c pyproject.toml packages/strategy_manager/tests -q
```

完整离线回归（命令对应[测试用例治理](TEST_GOVERNANCE.md)第 5 节，已换为 macOS 路径）：

```bash
.venv/bin/python -m pytest -c pyproject.toml tests -q
.venv/bin/python -m pytest -c pyproject.toml packages/strategy_manager/tests -q
.venv/bin/python -m pytest -c pyproject.toml packages/strategy_evaluator/tests -q
.venv/bin/python -m pytest -c pyproject.toml packages/factor_signal_catalog/tests -q
.venv/bin/python -m pytest -c pyproject.toml packages/strategy_template_catalog/tests -q
.venv/bin/python -m pytest -c pyproject.toml packages/paper_trading_engine/tests -q
node --test-isolation=none --test \
  packages/paper_trading_engine/tests/functional/console_state.test.mjs
.venv/bin/ruff check src tests \
  packages/factor_signal_catalog packages/strategy_template_catalog \
  packages/strategy_manager packages/strategy_evaluator \
  packages/paper_trading_engine/src packages/paper_trading_engine/tests
```

## 5. 能力地图（macOS 可运行的全部入口）

### 5.1 TDR（`.venv/bin/czsc-trader`）

| 命令 | 作用 | 是否需要外部服务 |
| --- | --- | --- |
| `catalog validate / list / show` | FSC 因子与信号目录查询 | 否 |
| `template validate / list / show / instantiate` | STC 策略模板目录 | 否 |
| `data validate` | 校验仓库已发布数据 | 否 |
| `data prepare` / `data update-backtest` | 从 Tushare 发布新行情 | 需要 `TUSHARE_TOKEN` |
| `strategy list / show / history / validate / performance` | 正式策略身份与证据 | 否 |
| `baseline list / show / validate` | 只读历史基线别名 | 否 |
| `backtest run` | 确定性因果回测，输出到 `outputs/` | 否（读取已发布数据） |
| `advice run` | 按持仓/资金生成 advice.v4/v5 决策，不连券商 | 否 |
| `archive validate --all` | 校验全部不可变实验档案 | 否 |
| `news extract` | MaaS 新闻事件抽取 | 需要 MaaS 凭据 |

### 5.2 PTE（`.venv/bin/pte`）

所有子命令都需要 `--repo-root`（macOS 下通常直接传 `.`）：

| 命令 | 作用 |
| --- | --- |
| `serve --repo-root .` | 启动常驻服务：调度器 + Web 控制台（前台运行，Ctrl+C 停止） |
| `once --repo-root .` | 执行一次数据/决策/对账刷新后退出 |
| `account list / create / pause / resume` | 虚拟账户管理（直接读写本机 SQLite） |
| `performance export` | 导出模拟盘里程碑证据包 |
| `control restart` | 请求运行中的服务做优雅退出（见 7.5，macOS 需手动重新启动） |
| `control repair-ledger` | 对运行中服务执行受保护账本修复 |

`serve` 关键默认值（来自 [cli.py](../packages/paper_trading_engine/src/paper_trading_engine/cli.py)）：
`--host 127.0.0.1 --port 8080 --order-interval 5 --account-interval 60
--decision-interval 5 --data-refresh-time 20:30 --data-start 2020-01-01
--opend-host 127.0.0.1 --opend-port 11111`。

本机状态默认位置：

- 数据库：`state/paper_trading/runtime.db`
- 运行时行情副本：`state/paper_trading/data/`
- 启动前备份：`state/paper_trading/backups/`（每次 `serve` 自动备份，滚动保留 3 份）
- 控制台图表缓存：`state/paper_trading/charts/`

以上目录均被 Git 忽略。

## 6. 场景一：离线研究、回测与决策（不需要 OpenD）

仓库已包含 `data/raw/`、`data/backtest/` 已发布数据，以下命令开箱即用：

```bash
# 查看/校验正式策略
.venv/bin/czsc-trader strategy list
.venv/bin/czsc-trader strategy show --strategy S001 --version v1

# 回测（结果写入 Git 忽略的 outputs/）
.venv/bin/czsc-trader backtest run \
  --strategy S001 --strategy-version v1 --dataset backtest \
  --symbol 588080.SH --asset etf \
  --start 2026-01-05 --end 2026-09-04 --init-cash 100000

# 按实际持仓和资金生成决策（不连券商、不下单、不改账户）
.venv/bin/czsc-trader advice run \
  --symbol 588080.SH --asset etf \
  --actual-quantity 0 --available-cash 100000 \
  --strategy S001 --strategy-version v1 \
  --format json
```

## 7. 场景二：启动 PTE 模拟交易服务

### 7.1 启动 Futu OpenD for Mac（需要实际模拟下单时）

先启动 OpenD 并登录**唯一**中国市场模拟账户，确认监听 `127.0.0.1:11111`。PTE 独占该底层
模拟账户；不启动 OpenD 也可以启动 PTE 查看控制台和历史数据，但渠道为降级状态。

### 7.2 前台启动（推荐的开发/学习方式）

```bash
.venv/bin/pte serve --repo-root .
```

启动成功后终端会打印 `PTE listening on http://127.0.0.1:8080`。新开一个终端验证：

```bash
curl -s http://127.0.0.1:8080/api/system/status | .venv/bin/python -m json.tool
```

浏览器打开控制台：<http://127.0.0.1:8080>

- 首次启动会幂等创建 `s001-v1` 账户（10 万元，绑定 S001-v1），并自动备份数据库；
- 停止服务：在运行服务的终端按 **Ctrl+C**（会触发优雅关闭，等待调度线程退出）；
- `serve` 与 `once` 对 `runtime.db` 使用操作系统级独占锁，**严禁同时启动两个写进程**，
  第二个实例会明确报错退出。

### 7.3 保存日志（可选）

macOS 上没有 Windows 服务的文件日志，进程输出直接到终端。需要留存时自行重定向到
Git 忽略的 `state/` 下：

```bash
mkdir -p state/paper_trading/logs
.venv/bin/pte serve --repo-root . 2>&1 | tee state/paper_trading/logs/pte.log
```

### 7.4 后台运行（可选，无自动拉起）

macOS 没有 WDG，后台进程崩溃后**不会自动重启**，这是与 Windows 生产形态的核心差别：

```bash
mkdir -p state/paper_trading/logs
nohup .venv/bin/pte serve --repo-root . \
  > state/paper_trading/logs/pte.log 2>&1 &
```

优雅停止后台进程（发 SIGINT 模拟 Ctrl+C，不要用 `kill -9`）：

```bash
kill -INT "$(lsof -ti tcp:8080)"
```

### 7.5 重启与账本修复

`pte control restart` 的语义是请求服务完成当前工作后**自行退出**。Windows 上由 WDG 自动拉起；
macOS 没有托管层，旧进程退出后不会有新实例，因此该命令在等待 `--wait` 秒后会报
“did not become healthy”超时错误——**这是预期现象，表示旧进程已被请求退出**，随后手动重新启动：

```bash
.venv/bin/pte control restart --repo-root . --wait 30   # 预期以超时报错结束
# 确认旧进程已退出后，重新启动：
.venv/bin/pte serve --repo-root .
```

日常更新代码后，通常直接在服务终端按 Ctrl+C 再重新 `serve` 即可，不一定要用 `control restart`。

受保护的账本修复对运行中服务操作，不涉及重启：

```bash
.venv/bin/pte control repair-ledger --repo-root . \
  --account-id s003-v1 --intent-id PTE-XXXXXXXXXXXXXXXXXXXX
```

### 7.6 虚拟账户管理（另开终端执行）

```bash
.venv/bin/pte account list --repo-root .

.venv/bin/pte account create --repo-root . \
  --account-id s002-v1 --name "S002-v1模拟账户" \
  --strategy S002 --strategy-version v1 \
  --symbol 510500.SH --asset etf --initial-cash 100000

.venv/bin/pte account pause  --repo-root . --account-id s002-v1
.venv/bin/pte account resume --repo-root . --account-id s002-v1
```

创建账户前 PTE 会通过 TDR CLI 做预检：验证策略发布为 `PAPER_READY`、数据截止日一致、
决策身份与冻结版本一致、费率一致；预检失败不会创建账户。

### 7.7 单次刷新

不想常驻服务时，可执行一次完整刷新（数据发布 → 决策 → 对账）后退出。同样受独占锁保护，
不能与 `serve` 同时运行：

```bash
.venv/bin/pte once --repo-root .
```

## 8. 状态检查与常见问题

检查端口与进程：

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
curl -s http://127.0.0.1:8080/api/status | .venv/bin/python -m json.tool
```

**Q：`python3 --version` 是 3.14，能直接建 venv 吗？**
不能。用 `python3.12 -m venv .venv`；已建错的按 2.3 节删除重建。

**Q：安装 vectorbt / numba / tsfresh 时编译失败？**
确认 `.venv/bin/python --version` 是 3.12 且 `file .venv/bin/python` 为 arm64；升级 pip
（`.venv/bin/python -m pip install --upgrade pip`）后重装。不要用 sudo 或 conda 基础环境混装。

**Q：启动 PTE 报 `8080 is already in use`？**
说明已有服务在跑。用 `lsof -nP -iTCP:8080 -sTCP:LISTEN` 找到进程；若它是旧 PTE，
用 7.4 节的 `kill -INT` 优雅停止，不要直接启动第二个实例。

**Q：启动 PTE 报数据库锁错误？**
另一个终端可能在跑 `pte serve` 或 `pte once`。同一时刻只允许一个写进程。

**Q：控制台显示 Futu 渠道降级 / `DEPENDENCY_DEGRADED`？**
Futu OpenD 未启动或未登录模拟账户。启动 OpenD 后 PTE 会自动重连，无需重启服务；
降级期间不能提交订单，但控制台、历史数据和审计事件可正常查看。

**Q：`pte-watchdog` 命令报错？**
它是 Windows 专属服务入口，macOS 不支持，忽略即可。

## 9. macOS 使用红线

- 不要直接修改 `state/paper_trading/runtime.db`；运维修复只能走 `control repair-ledger`
  等带证据校验和审计事件的正式入口。
- 不要同时运行两个 `serve`，或 `serve` 与 `once` 并发。
- 不要在未核对 Futu 订单、成交、持仓的情况下，跨机复制 `state/` 后直接下单。
- 测试保持离线、确定性，临时产物只能进 `.tmp/`，不要写进 `data/`、`outputs/`、`experiments/`。
- 业务边界与完整操作语义以[技术交接](DEVELOPMENT_HANDOFF.md)和[用户使用说明](USER_GUIDE.md)为准。
