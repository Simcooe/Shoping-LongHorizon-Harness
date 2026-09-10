# 阶段5：硬预算、Ask Route、失败处理与 Session 保存

## 任务

把论文要求的角色预算与控制路由变为 runtime 事实，同时修复全量实验暴露的基础设施
可靠性问题。

## 前置

阶段1-4完成。保持 `mea-v4-paper` 隔离，不改变已冻结 profile。

## 硬预算

- `allowed_tools` 在 pre-execute 强制执行。
- `max_tool_calls` 达到后结束当前 Executor episode，不允许继续调用。
- Executor、Manager、Auditor 各自有独立 timeout。
- 最大轮数默认25以对齐论文；购物消融可另设10，但必须写入 manifest。
- timeout/max-round/budget 结果必须成为结构化状态，不能表现为 `running`。

## Manager Ask Route

Manager decision 增加 `ask`：

```json
{
  "decision": "ask",
  "question": "一个具体问题",
  "reason": "为什么缺少该信息会阻塞下一 contract"
}
```

由 controller 调用 shopper simulator，回复进入 requirement/state；不要把 ask 伪装成普通
Executor搜索 round。

## 失败处理

- Manager/Auditor transient overload 使用指数退避和有限重试。
- 连续失败后任务必须标记 failed，不得写入 stats.done。
- runner resume 只能跳过完整且协议校验通过的 task。
- 保存每轮 Executor原始 session、Manager/Auditor请求日志、state、contracts、audits。
- manifest 记录模型、预算、超时、最大轮数、并发、重试和每任务来源。
- 中断后可恢复到最后一条 clean audit，不复用未审计的 Executor report。

## 测试

- 超过 allowed_tools/max_tool_calls 被硬拒绝。
- Manager ask 不启动 Executor。
- 一次 overload 后重试成功。
- 连续失败后 runner status=failed。
- Ctrl-C 后 resume 不跳过半成品。
- 每个完成任务均有原始 session 和完整协议产物。
- 终局后购物动作计数为0。

## 完成条件

以并发1和并发2各跑一组8条任务，不能出现被误记为done的角色错误或空 session。

