# 阶段1：运行归档、会话身份与事件契约

## 任务与前置阅读

先读总览，再读 `scripts/run_benchmark.py`、`scripts/export_trace.py`、
`src/shop-tools.js`、`scripts/shopper_simulator.py`。
本阶段提供新runtime的基础组件和fixture；不改变mea-v3购物策略。
建议实现于 `src/mea-v4/`，具体文件划分服从已有DSH接口。

## 会话身份

区分task_id、run_id、attempt_id、round_id、episode_id、environment_session/lease、
shopper_session。DSH每轮新episode，ShopSimulator和Shopper在同一attempt内连续；
新attempt使用新的Shopper历史，不继承上次尝试的问答。

先修正并测试身份传递契约：当前runner预热Shopper使用run/task，而
`shop-tools.js`的提问键使用task_id，两端不能各自拼出不同的session键。
为新profile显式传入统一会话键；共享工具改动需保证旧profile默认行为与新行为有版本区分。
每次/start只发生在attempt初始化，不能每轮重置问答。

## 任务级事件契约

提供追加写入的task event journal与校验器，至少包含：

- task/run/attempt/round/episode来源、role与事件类型；
- 全局单调sequence、唯一event_id、tool_call_id/request_id；
- 工具参数、对应结果、模型可见文本和隔离的原始返回；
- Manager发起的真实question/reply以及来源，不能伪装成Executor调用；
- episode开始/结束、budget/timeout、审计结果引用、实际模型usage（若可得）。

请求/结果按call_id匹配，不能靠“上一个pending调用”或局部step编号匹配。
缺结果、重复结果、并行返回乱序必须显式记录；不得静默丢事件。
只归档环境实际返回的done，不能把Manager的done写成raw.done。

原始执行轨迹、角色请求/响应和审计日志按episode持久保存；跨轮模型输入使用受控投影。
费用/token不可用时为null，不能用工具次数估算成实际费用。

## 运行状态与恢复前置契约

运行完成、环境终局、用户任务成功分别记录。角色错误、导出失败、空session不能因为进程
退出或trace文件存在就算运行成功。blocked/max_rounds属于可记录的任务停止状态，不等于
基础设施异常，也不等于购买成功。

resume只跳过完整、输入指纹匹配且协议验证通过的attempt。
检查checkpoint和journal是否存在未完成的调用、尚未审计的环境修改。
若当前环境/lease身份无法核实，返回recovery_required；不得仅凭旧env_idx继续。
本阶段只定义恢复状态与测试输入，阶段5实现实际环境协调。

中断时停止调度、终止并等待该attempt的子进程退出、保存已收到的事件，再按lease所有权
释放资源。历史失败attempt不覆盖，后续重试记录新attempt与替换原因。

## 验收与交接

用fixture验证：

- 同一attempt跨轮共享Shopper，两个attempt互不串话；
- 多调用乱序返回、重复step、多session均能唯一对应；
- 导出失败/缺session不会被记为运行成功；
- Ctrl-C保留日志和中断状态，不把半成品当resume命中；
- session标识、事件schema和原始日志路径可被后续adapter直接调用。

交付接口定义、fixture和测试命令。暂不启动真实Manager/Auditor或全量任务。
