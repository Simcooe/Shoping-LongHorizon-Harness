# 阶段5：真实MEA闭环、Ask路由与环境协调恢复

## 前置与范围

阶段1-4通过。接通真实Manager、Executor adapter、Auditor adapter和reducer，
形成新profile mea-v4-paper的任务控制层。使用阶段2定义的schema，不另造不兼容协议。

## Manager与调度

Manager独立模型请求读取原始公开任务、当前状态、累积审计报告和真实shopper回复，
通过state_updates提议更新；reducer验证后生成execute/done/blocked/ask分支。
Manager无环境工具，Executor report不能直接更新Manager的完成事实。

execute -> 新Executor episode -> 独立Auditor -> reducer校验Manager更新 -> 下一决策。
无效JSON、无效更新和transient错误有有限重试和有界退避；记录全部尝试。
先核对overload来源，不能把每个CPU overload都假定成模型服务限流或擅自关闭保护。

ask由controller调用Shopper，使用阶段1的统一attempt会话键；保存真实问答事件，
更新需求版本后重新规划。此分支不启动Executor，不消耗购物环境step，
但纳入问答次数、模型usage及全局时间预算。设置问答/规划循环上限，避免ask无限循环。

## 结束与预算

区分environment_done、harness_outcome与runtime_status：

- environment_done：停止购物工具，仍允许独立最终审计；保留第一次真实终局。
- harness_outcome：audited_success、blocked、budget_exhausted、unresolved等。
  成交或Executor自称完成不能替代最终审计。
- runtime_status：completed/failed/interrupted等，基础设施错误单列。

基础论文核心版本允许执行contract内的购买；不要求purchase_authorization。
购买后如果审计发现不满足，就记录未完成，不能恢复购物去“修复”已经终止的episode。
Auditor最终审计失败时记录runtime失败，不能仅凭环境done标成功。

对照主配置先保持mea-v3的10轮和相同环境总步数；独立预算参数必须显式记录。
论文25轮及1800/300/300秒参数作为另一个配置，不称为全部购物实验的必需默认值。

## 恢复：核对真实环境后继续

最后clean audit只是已知事实的checkpoint，不代表环境回到那个时刻。
从journal发现未审计动作后：

1. 验证lease、environment_session、attempt及Shopper历史一致性；
2. 停止旧Executor子进程，独立inspect当前环境；
3. 对尚未审计的变化执行恢复audit，重新校验状态和依赖；
4. 已有真实终局时只做最终审计，禁止再次购买。

如果购买请求发出但未收到响应，先查询实际成交/终局；未知时标recovery_required，
不能直接重发Buy Now。会话丢失/slot已重用则结束该attempt或显式重启整项任务；
保留旧attempt，禁止把新环境接在旧Task State后面。
不要求本项目此时实现环境回滚/持久化快照；无法恢复必须诚实报告。

## 验收与交接

验证真实Manager产生contract、ask往返、部分audit更新、预算停止、最终只读审计。
注入overload、执行中断、购买响应丢失、lease重用，证明不会误记成功/重复购买。
在开发任务上验证并发1/2的attempt隔离与完整session归档。
完成时记录adapter限制、API版本、role模型参数、预算和实际测试结果。
