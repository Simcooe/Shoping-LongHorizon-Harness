# 阶段3：Fresh-Context Executor、预算与跨轮Trace导出

## 前置与范围

阶段1-2通过。阅读论文2.3及DSH实际session/episode接口。
实现薄AgentAdapter，保留DSH原生规划/工具循环；不另写替代DSH的CLI Agent。
本阶段用stub Manager和Auditor驱动两轮；真实闭环由阶段5接线。

## Episode接口

runEpisode(role, taskContext, contract, environmentHandle, budget)返回
结构化report、运行状态、episode_id及日志位置；错误与预算耗尽有明确返回值。
接口覆盖角色隔离需求，先实现Executor；只支持一个DSH后端即可，不要求一次接多个产品。

每轮Executor只接收原始公开任务、相关Task State、当前contract、被引用的prior audits、
工具schema与公开环境入口。审计日志和旧session不暴露为文件读取能力。
跨轮丢弃原始assistant/tool消息及推理上下文；日志仍归档供离线检查。

使用新DSH session或有证据证明等价的独立episode；验收依据是实际发出的模型请求，
不是进程数。禁止自动resume/隐式载入旧历史。不同进程本身也不能证明上下文隔离。
环境和Shopper在同一attempt内不reset，lease由controller持有。

## 预算与日志

从现在开始执行独立timeout与工具预算，不能等全部真实case跑完再补硬限制。
工具权限由runtime执行；拒绝调用、控制工具和购物工具的计数口径写清楚。
工具名称白名单、细粒度参数限制分开定义；自然语言边界由审计检查，不能宣称全部可硬拦截。
保存每轮原始session与请求检查证据，终止episode后等待子进程退出再推进下一轮。

## 导出接口

扩展/新增任务级exporter，把阶段1 journal与各episode实际事件按global sequence合并。
输出配对model_trace/raw_trace，保证：

- 原始local_step保留在来源字段，跨轮引用使用唯一的导出step/event_id；
- tool call/result依call_id对应；两条轨迹事件顺序和参数一致；
- task字段保留原始用户任务，不能被第二轮contract替换；
- controller真实问答可导出为兼容的ask_shopper事件，并显式标记actor=manager/controller；
  问答必须实际发生，不得伪造为Executor调用；
- Manager/Auditor提示、总结、内部审计结论及最终audit不注入离线Judge的购物轨迹；
- 只读audit操作写入独立审计journal/成本统计，不算购物step；
- raw.done保持第一次实际环境终局，不被Harness内部done替换。

优先让导出投影兼容现有Judge需求激活规则。若必须改评测schema/工具分类，记录迁移，
用历史fixture验证指标不漂移，不能静默调整已有评价口径。

## 验收与交接

- 捕获第2轮实际请求：第1轮私有测试标记/工具历史不可见，引用的audit可见。
- 两轮修改同一模拟环境但session历史隔离。
- 真实controller问答导出后被现有Judge识别，TaskFact激活依据指向真实回复。
- 跨轮重复local_step、乱序返回、控制事件均正确配对；第一raw.done位置正确。
- tool budget/timeout确实停止episode并保存日志。
- 完成一次两轮mock端到端导出，再用1条非Final-200开发任务验证环境连续性。
