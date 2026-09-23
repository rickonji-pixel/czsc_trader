# S008 EX07 执行记录

按预注册脚本启动后，在读取任何价格文件前被manifest边界断言阻断。518880.SH的权威manifest按治理
要求同时覆盖开发池和封存池，而脚本错误要求manifest的`requested_end`必须等于开发池截止日；因此
抛出`ValueError: manifest exceeds development cutoff`并停止。
