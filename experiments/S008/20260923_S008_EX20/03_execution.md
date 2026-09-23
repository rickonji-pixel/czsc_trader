# S008 EX20 执行记录

实验在第一套原型调用`StrategyInstance.prepare_data()`创建隔离数据子目录时，被Windows沙箱以
`PermissionError: [WinError 5]`阻断；随后`TemporaryDirectory`清理同一目录也因ACL被拒绝。
阻断发生在任何真实市场数据读取、收益计算、参数搜索或TXE执行之前。

