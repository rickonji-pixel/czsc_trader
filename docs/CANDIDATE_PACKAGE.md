# 策略候选包

策略研究员负责实现并交付完整候选包。投资总监通过平台工具完成候选审查、体检、冻结和SRT部署。

## 存放位置

候选包必须存放在：

```text
research/SXXX/candidates/SXXX-Cnnn/
```

平台受理后，将经过验证的原始内容封存到：

```text
strategies/SXXX/candidates/SXXX-Cnnn/package/
```

策略研究员不得直接修改治理区内容。

## 目录结构

```text
SXXX-Cnnn/
├─ candidate_submission.json
├─ candidate_snapshot.json
├─ runtime_binding.json
└─ runtime/
   └─ strategy_runtime/
      ├─ strategies/
      │  └─ sxxx_cnnn.py
      ├─ charts/
      │  ├─ __init__.py
      │  └─ sxxx_cnnn.py
      └─ resources/
         └─ ...
```

`runtime/strategy_runtime/`中的相对路径就是冻结后安装到SRT子包的目标路径。

## candidate_submission.json

```json
{
  "schema_version": 1,
  "candidate_snapshot": "candidate_snapshot.json",
  "runtime_binding": "runtime_binding.json",
  "runtime_root": "runtime/strategy_runtime",
  "files": {
    "candidate_snapshot.json": "<sha256>",
    "runtime_binding.json": "<sha256>",
    "runtime/strategy_runtime/strategies/sxxx_cnnn.py": "<sha256>",
    "runtime/strategy_runtime/charts/__init__.py": "<sha256>",
    "runtime/strategy_runtime/charts/sxxx_cnnn.py": "<sha256>"
  },
  "package_hash": "<sha256>"
}
```

`files`必须覆盖候选包内除`candidate_submission.json`以外的全部文件。`package_hash`是去掉
`package_hash`字段后，对完整manifest对象计算的规范JSON SHA-256。

## candidate_snapshot.json

```json
{
  "schema_version": 1,
  "strategy_id": "SXXX",
  "candidate_id": "Cnnn",
  "source_experiment": "experiments/SXXX/<experiment-id>",
  "strategy_payload": {
    "runtime": {
      "module": "strategy_runtime.strategies.sxxx_cnnn",
      "qualname": "SXXXCandidate",
      "contract_version": 1,
      "source_files": ["strategies/sxxx_cnnn.py"],
      "source_sha256": "<sha256>"
    },
    "parameters": {}
  },
  "data_contract": {"requirements": []},
  "execution_policy": {"policy_type": "FROZEN_RULE"},
  "research_claims": {"summary": "候选策略主张"},
  "candidate_hash": "<sha256>"
}
```

`candidate_hash`是去掉`candidate_hash`字段后，对完整候选快照计算的规范JSON SHA-256。
`strategy_payload`中的实现和参数是体检、冻结及部署过程中保持不变的可执行身份。

## runtime_binding.json

```json
{
  "schema_version": 1,
  "candidate_id": "SXXX-Cnnn",
  "source_files": [
    "strategies/sxxx_cnnn.py"
  ],
  "implementation_sha256": "<sha256>",
  "install_files": [
    "strategies/sxxx_cnnn.py",
    "charts/__init__.py",
    "charts/sxxx_cnnn.py"
  ],
  "charts": {
    "module": "strategy_runtime.charts.sxxx_cnnn",
    "qualname": "SXXXCharts",
    "contract_version": 1,
    "source_files": [
      "charts/sxxx_cnnn.py"
    ],
    "source_sha256": "<sha256>"
  }
}
```

策略实现必须继承`StrategyImplementation`，同时实现`from_candidate`和`from_release`。候选
`strategy_payload.runtime`声明的模块、类、源码闭包和源码哈希必须与binding一致。

图表类必须实现：

```python
class SXXXCharts:
    def render_backtest(self, context): ...
    def render_forward_observation(self, context): ...
```

两个方法分别生成TDR回测图和PTE前瞻观察图。图表代码只能读取平台传入的context，不得直接
读取TDR输出目录、PTE数据库、券商接口或SRT私有准备目录。

两个方法统一接收`strategy_chart.v1`上下文。平台负责提供策略身份、窗口、独立行情、策略输出、
执行账本和渲染选项；策略图表代码负责把这些事实解释为本策略的分面、阈值、信号和状态语义。
两个方法必须返回完整HTML文档。候选审查会校验图表源码闭包和哈希；缺少契约、身份不一致或
渲染失败都会直接阻断审查或运行，不存在平台旧图回退路径。

## 投资总监操作

最终EvaluationMandate由投资总监维护在候选包目录之外，避免与研究员提交的不可变候选包混合。

```powershell
.\.venv\Scripts\czsc-trader.exe candidate review `
  --package research/SXXX/candidates/SXXX-Cnnn `
  --mandate research/SXXX/mandates/SXXX-Cnnn.json

.\.venv\Scripts\czsc-trader.exe candidate evaluate SXXX-Cnnn

.\.venv\Scripts\czsc-trader.exe candidate freeze SXXX-Cnnn `
  --change-summary "策略版本说明"

.\.venv\Scripts\czsc-trader.exe strategy deploy SXXX-vN
.\.venv\Scripts\czsc-trader.exe strategy list SXXX
.\.venv\Scripts\czsc-trader.exe strategy info SXXX-vN
```

`candidate freeze`只生成不可变策略版本包。`strategy deploy`成功后，该版本才进入SRT，并能被
`strategy list`和`strategy info`查询。
