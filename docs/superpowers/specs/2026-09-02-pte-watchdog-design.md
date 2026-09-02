# PTE Watchdog 设计

## 目标

使用逻辑极简的 Windows 系统服务 `CZSC-PTE-Watchdog` 确保 PTE 开机自启动，并在 PTE 退出或 HTTP 服务失效后自动恢复。

## 进程边界

- watchdog 是唯一注册到 Windows SCM 的服务，启动类型为自动。
- PTE 仍通过现有 `pte serve` CLI 作为 watchdog 子进程运行。
- watchdog 不导入交易引擎、券商网关、调度器或 Web 服务实现。
- watchdog 的配置只描述仓库路径、PTE 命令、HTTP 探活地址和探活参数。

## 生命周期

1. watchdog 启动 PTE 子进程，并将工作目录设为仓库根目录。
2. 每 10 秒检查子进程状态和 `http://127.0.0.1:8080/api/status`。
3. 连续 3 次探活失败时，先终止旧进程，再按 5、30、60 秒退避重启；后续保持 60 秒。
4. 探活成功后清零连续失败数和退避级别。
5. SCM 请求停止时，watchdog 终止 PTE 并等待退出，必要时强制结束。

## 日志与配置

- watchdog 日志：`state/paper_trading/logs/watchdog.log`，5 MB，保留 5 份。
- PTE 标准输出和错误：`state/paper_trading/logs/pte.log`，追加写入。
- 配置：`state/paper_trading/service.json`，保存绝对仓库路径和既有 PTE 参数。

## 安装与迁移

- `pte-watchdog install-config --repo-root <path>` 以自动启动方式安装服务并配置 SCM 自身恢复策略。
- 安装时删除旧的 `CZSC-PaperTrading` 服务，避免两个运行主体争用 8080。
- 服务显示名为 `CZSC Paper Trading Watchdog`。

## 验收

- Windows 服务状态为 Running、启动类型为 Auto。
- 8080 的 `/api/status` 可访问，暂停和恢复接口有效。
- 子进程退出或连续三次 HTTP 探活失败后，watchdog 重新拉起 PTE。
- PTE 的交易时间窗、19:00 数据发布和账户可用资金定仓逻辑保持不变。
