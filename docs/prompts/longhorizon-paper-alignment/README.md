# LongHorizon-Harness 论文对齐实施总览

## 目标

在不修改已冻结的 h0、mea-v3 代码语义和评测产物的前提下，新建
`mea-v4-paper`，实现论文的核心语义：

```text
Manager
  -> 结构化、可追溯的 Task State 与有界 Contract
  -> fresh-context Executor 修改环境
  -> independent read-only Auditor 重新检查环境
  -> 只有 clean audit evidence 可以推进 Task State
  -> 下一轮
```

论文来源：`reference/LongHorizon-Harness.pdf`。

## 为什么不直接修改 mea-v3

`evaluations/mea-v3/` 已经形成200条正式结果。直接改变 mea-v3 会导致代码与历史
结果不再对应。所有论文对齐改动必须进入新 profile 和新状态 schema；旧 profile、
trace、Rubric、Judge 与 report 保持可复现。

## 当前 mea-v3 与论文的关键差距

1. Executor 在一个持续增长的 DSH 会话中运行，并非每轮 fresh context。
2. Auditor 没有只读环境工具，只复核 Executor 本轮提供的工具结果。
3. Task State 不是 requirement/artifact/fact 的结构化证据状态机。
4. Contract 缺少依赖、边界约束与相关证据引用，工具预算也是软约束。
5. `Buy Now` 可以在 Auditor 核验前使环境不可逆终止。
6. Manager 缺少独立 `ask` route。
7. 没有统一 AgentAdapter 去启动独立的角色 episode。
8. Manager/Auditor 过载可能被 runner 误记为任务成功，原始 DSH session 也未保存。

## 实施顺序

严格按下列顺序执行，每个文档交给一个独立会话。不要并行修改重叠文件。

| 阶段 | 文档 | 结果 |
|---|---|---|
| 1 | `01_STATE_AND_CONTRACT.md` | 结构化 Task State、Contract、Audit reducer |
| 2 | `02_AGENT_ADAPTER_FRESH_EXECUTOR.md` | 真正独立的每轮 Executor episode |
| 3 | `03_READ_ONLY_AUDITOR.md` | Auditor 独立读取环境，而非复用 Executor Observation |
| 4 | `04_AUDITED_PURCHASE.md` | 购买授权与完成审计，解决不可逆终局绕过问题 |
| 5 | `05_RUNTIME_CONTROL_AND_RELIABILITY.md` | 硬预算、ask route、重试、runner/session 完整性 |
| 6 | `06_VALIDATION_AND_EXPERIMENT.md` | 论文语义验收、小批次与全量配对实验 |

## 全局边界

- 不修改 `evaluations/h0/`、`evaluations/mea-v3/` 和冻结 Rubric。
- 不修改 mea-v3 的历史语义来迎合结果；新机制只进入 `mea-v4-paper`。
- 在线 Manager/Auditor 不得读取 reward、gold、私有 TaskFacts、deterministic、Judge
  或 report。
- Auditor 只能读取公开环境状态，不得修改购物状态。
- Executor report 永远是未验证声明，不能直接把状态标为 completed。
- 不把测试 mock 当成真实能力；每阶段必须说明实际运行了哪些测试。
- 不跑完整200条，直到阶段6的小批次门槛全部通过。

## 论文对齐的最终判定条件

只有以下条件同时成立，才能称为 paper-aligned：

1. 每轮 Executor 请求中不存在前一轮 assistant/tool 原始历史。
2. 跨轮仅持久化结构化 Task State、Contract 与 Audit Report。
3. Auditor 的关键结论来自自己调用的只读环境工具。
4. completed 状态全部有 clean audit evidence 引用。
5. 最终完成必须满足 audit status=`complete` 且 integrity=`clean`。
6. 所有角色通过 AgentAdapter 以独立预算运行。
7. 运行时硬限制 contract 工具集、工具次数、时间与最大轮数。
8. 失败的 Manager/Auditor/Executor 不得被 runner 记为 done。

