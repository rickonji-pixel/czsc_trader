# 实验档案

本目录保存可审计、受Git跟踪的正式研究实验。实验按SM策略ID分区：

```text
experiments/
├── S001/
├── S002/
├── S003/
└── S004/
```

新实验路径统一为`experiments/<策略ID>/YYYYMMDD_<策略ID>_EXnn/`。实验ID在整个仓库内
保持唯一；同一策略、同一天从`EX01`开始递增。跨标的验证仍归属于被验证的策略，不按标的
另建目录。

每个实验包含四份研究文档、实验专属编排、`artifacts/`机器证据和
`experiment_manifest.json`。清单完成后，实验档案保持不可变；发现错误时创建新实验并声明
继承关系。可重建中间文件和运行缓存统一写入`.tmp/`，不得进入实验档案。

验证全部实验：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root .
```

历史SM版本和证据可能保留迁移前的`experiments/<实验ID>/...`来源字符串，以维持发布哈希
和审计记录。TDR通过全局唯一实验ID解析到当前策略目录。

历史`run_experiment.py`保留为当时执行代码，不保证在迁移后的目录中直接运行。需要继续同一
问题时创建新实验，显式声明来源实验及其哈希，并通过TDR的当前公共能力重建证据。
