# 模块边界记录

本文记录已经确认并落地的模块边界，避免后续重新引入已删除的兼容层。新增跨模块职责前必须先
确认业务所有权，再进入独立设计和实现任务。

## DFLS与SRT

- DFLS负责单项数据请求的获取、规范化、统一校验、按“供应商＋标的”修复和失败阻断；成功
  返回`DataResult`及其内容身份。
- SRT负责一个策略实例的多输入范围推导、组合认证和持久化。`StrategyInstance.prepare_data()`
  是唯一策略数据准备入口。
- 初始信号日、历史回看窗口、因果滞后和增量特征构造属于`StrategyImplementation`，主调方
  不传入或解释策略数据集。

## SRT与TDR

- TDR拥有普通回测和审核快照的执行行情、评价窗口、初始资金、审计、报告和图表。
- TDR为每次运行分配隔离数据目录，创建`StrategyInstance`并显式调用`prepare_data()`；策略依赖
  由实例自行准备。
- 历史执行由`StrategyInstance.run_window(...)`驱动TXE的`HistoricalExecutor`。TDR不维护按策略
  ID分支的规则解码器，也不把诊断字段当成强制决策字段。
- `generation_integrity.py`和`data_service.py`中的generation只描述TDR执行数据池事务，不属于
  SRT策略数据制品。

## SRT与PTE

- PTE在每个交易日20:30后为每个运行账户创建隔离`StrategyInstance`并显式调用
  `prepare_data()`；准备成功后立即计算该账户决策。账户之间的数据空间、进度和失败退避相互隔离。
- PTE只提供账户数据目录、交易窗口、现金、持仓和修订号，通过SRT公共接口准备和恢复实例并
  核对`data_identity`，不解析`prepared-data.json`或任何策略输入。
- SRT拥有目标仓位、参考价、资金规则、委托参数和生效时点；PTE提供账户现金、持仓和修订号，
  持久化计划并依据Futu回报执行和记账。
- 旧的publication、generation运行上下文、Runner和双轨平面发布接口已经删除，不提供兼容路径。

## 当前状态

SRT、TDR、TXE和PTE的本轮边界收口已经完成。后续工作属于版本交付：全量回归、文档同步、合并、
打tag以及经单独授权后的PTE生产发布；当前没有已知的接口迁移待办。
