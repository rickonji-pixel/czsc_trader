# S003 EX46 设计

搜索偏差审计纳入S003历次收益实验形成的142条路径，按交易日补零对齐，并使用12 bp压力
成本收益。审计包括10块CSCV/PBO、原始及有效试验数DSR、10/21/42日三档平稳区块
Bootstrap、所有非零交易日循环错位、逐年删除、最近252日，以及75%/80%/85%三个资金流
宽度阈值邻域。

各方向输出`FAVORABLE/MIXED/ADVERSE`证据标签；任一方向为`ADVERSE`则总标签为
`ADVERSE`，全部为`FAVORABLE`才为`FAVORABLE`，其余为`MIXED`。本轮不创建候选，
也不修改SM或PTE。
