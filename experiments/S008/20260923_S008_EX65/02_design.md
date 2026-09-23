# S008 EX65 实验设计

EX65只替换正式搜索所使用的运行时来源：XAU/XAG采用`LATEST_AVAILABLE`与严格前值`merge_asof`，
并继承EX64的真实数据、陈旧度和EX53逐值一致证据。搜索仍为1,500次NSGA-II、InMemoryStorage、
8个spawn进程、种子`2026096004`、单边10bp/30bp；不早停、不扩预算、不选冠军、不读封存期。
