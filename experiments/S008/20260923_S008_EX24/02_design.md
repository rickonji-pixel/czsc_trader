# S008 EX24 实验设计

1. EX23的`EXECUTION_ERROR`及其证据保持不变；
2. 逐字复用EX23运行时源码，源码闭包身份必须相同；
3. 仍调用真实`StrategyInstance.prepare_data()`完成准备，只在返回给实验记录器的代理对象上暴露
   `definition.inputs.requirements`用于计数；
4. 其余合成数据、三套原型、SRT/TXE执行和裁决逻辑完全复用，不得修改。

