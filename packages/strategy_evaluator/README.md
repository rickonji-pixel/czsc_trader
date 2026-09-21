# 策略评估器（Strategy Evaluator，SE）

SE是确定性、渠道无关的数值评估包。它接收结构化候选、收益序列、交易账本和评价策略，输出
可复现的筛选、排名与稳健性数值证据。SE没有语义理解能力，也不读取仓库、加载行情、运行策略、
管理StrategyFamily/SGC/StrategyVersion或连接PTE。

公开纯函数工作流为 `validate_protocol` → `screen_candidates` → `rank_candidates` →
`finalize_evaluation` → `render_summary`。OPC-v1/v2按净年化、最大回撤、卡玛和盈亏比比较候选；
实验可以收紧阈值，不能降低既定协议要求。

OPC-v3在筛选和排名后，对TDR提供的事实执行PBO、DSR、绝对及配对区块Bootstrap、参数邻域、
成本压力和外部复现等确定性计算。`FAVORABLE`、`MIXED`、`WEAK`、`ADVERSE`是数值证据标签，
不等于人工投资判断或正式冻结裁决。

完整冻结体检由TDR依据EvaluationMandate组织：TDR指定评价窗口并调用SRT准备和认证策略数据，
再核验TXE成交账本、证据身份、SRT运行时、监测方案及所有必需审计项，并把SE的数值结果纳入
AdjudicationReport。

评价协议由 TDR 根据已批准的研究协议和 `EvaluationMandate` 显式传入。SE 只执行协议中声明的
硬门槛；PBO、DSR、Bootstrap、参数邻域和成本压力等结果在未被协议指定为门槛时属于诊断证据，
不能自行升级为冻结否决条件。

## 包级验证

```powershell
.\.venv\Scripts\python.exe -B -m pytest -c pyproject.toml packages/strategy_evaluator/tests -q
.\.venv\Scripts\python.exe -m ruff check packages/strategy_evaluator/src packages/strategy_evaluator/tests
```
