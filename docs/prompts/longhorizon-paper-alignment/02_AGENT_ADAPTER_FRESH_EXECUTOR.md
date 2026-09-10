# 阶段2：AgentAdapter 与 Fresh-Context Executor

## 任务

实现每轮独立 Executor episode。不能只在原会话末尾追加新 contract；必须证明发给
Executor 模型的请求不含此前轮次的 assistant/tool 原始历史。

## 前置

阶段1已完成且测试通过。先读总览、论文 Method 2.3、当前 `scripts/run_benchmark.py`
和 DSH 实际可调用接口，不要凭想象发明 API。

## 目标结构

```text
Task Controller
  -> AgentAdapter.runEpisode(role, context, tools, budget)
      -> 启动一个新的 DSH Executor episode
      -> 复用同一个 ShopSimulator env_idx/session
      -> 返回 executor_report + 运行诊断
```

建议新增：

```text
src/mea-v4/agent-adapter.js
src/mea-v4/controller.js
scripts/run_mea_v4_task.mjs
scripts/test_mea_v4_fresh_executor.mjs
```

## 每轮 Executor 只能接收

- original task；
- 当前结构化 Task State 的 contract 相关子集；
- 当前 Contract；
- Contract 明确引用的 prior Audit Reports；
- 当前角色工具 schema；
- 当前公开环境入口状态。

不得接收：前轮 assistant messages、tool calls、tool observations、内部 reasoning 或整个
audit history。

## 实现约束

- 优先使用真实 DSH/Agent backend 接口启动新 session；同进程消息过滤只能作为有测试
  证明等价的后备实现，并须在文档中标为 adapter limitation。
- 每轮拥有独立 timeout、tool budget、日志和 episode id。
- ShopSimulator lease 由 task controller 持有，跨轮不 reset。
- Executor只负责当前 Contract，不自行维护全局完成状态。
- 结果写入 `executor/<round>/`，跨轮输入不得直接读取这些原始文件。

## 测试

- 捕获第2轮模型请求，断言不存在第1轮 assistant/tool 内容。
- 原始目标、Task State 子集、Contract 和引用的 audits 存在。
- 未引用 audit 不进入上下文。
- 同一任务所有 round 使用相同 env_idx。
- 新 round 使用不同 executor episode/session id。
- 每轮只暴露 Contract allowed_tools。
- h0/h1/mea-v1/v2/v3 行为不变。

## 完成条件

至少用 mock backend 跑两轮并证明上下文隔离；再用1条真实购物任务证明环境状态跨 fresh
Executor episode 保留。不要运行 Final-200。

