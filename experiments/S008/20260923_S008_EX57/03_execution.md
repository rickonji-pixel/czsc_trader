# S008 EX57 执行记录

技术后继协议在提交`2ce30b72`冻结后执行。首次运行被沙箱对`.tmp`目录的ACL拒绝；提升仓库临时
目录写权限后原样重跑。输入合同、数据准备、状态机和单边10bp/30bp的TXE窗口执行均已完成，
但证据整理读取了旧字段名`dataset_identity`，当前`DataPreparationResult`只暴露`data_identity`，
因此在归档证据前停止。
