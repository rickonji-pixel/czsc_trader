# S008 EX14 执行记录

按预注册脚本启动并通过EX13档案、DFLS输入身份和目标行情manifest检查后，在月度宏观因果对齐
阶段被边界断言阻断。2024年11—12月参考值按M+2规则应在2025年才生效，脚本错误地要求所有
输入月份必须在开发池内找到生效交易日，因此抛出`ValueError: monthly availability exceeds
development sessions`并停止。没有生成特征面板或未来收益标签。
