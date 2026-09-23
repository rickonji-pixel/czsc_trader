# S008 EX63 实验设计

运行时、DFLS窗口、锚点参数和裁决规则全部继承EX62。唯一修正是把XAU/XAG陈旧度证据计算从
Series的`.days`改为`.dt.days`；策略计算和输入合同不变。
