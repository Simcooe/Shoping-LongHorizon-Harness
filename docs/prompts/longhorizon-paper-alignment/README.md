# LongHorizon-Harness 论文对齐实施总览（审查修订版）

## 目标与依据

新建 `mea-v4-paper`，在 Shopping 场景实现论文的外置任务状态、fresh-context
Executor、独立只读 Auditor，以及基于审计证据的状态推进。
依据为 `reference/LongHorizon-Harness.pdf` 第2节、图2及第3.1节。
本方案按论文描述设计；未逐项核对论文官方仓库，不能声称代码级复现。

明确区分三类改动：

- 论文核心：Manager维护状态并生成contract；Executor每轮独立上下文；Auditor独立
  检查环境；原始执行历史不作为跨轮模型记忆；完成判断建立在审计证据上。
- 购物适配与工程保障：公开inspect接口、多session事件导出、会话隔离、日志、预算执行、
  失败分类与恢复。这些细节需结合现有DSH实现，不声称论文提供了相同接口。
- 可选扩展：购买前授权门禁。论文没有规定这种两阶段购买协议，单独实现和评估。

## 已有结果与改动边界

`evaluations/h0/`、`evaluations/mea-v3/`、原始runs和冻结Rubric保留。
新profile使用独立模块和配置；修改共享环境/API/runner时提供回归测试与版本记录。
保存当前源码、配置和依赖版本指纹；只有换profile名称并不能冻结共享代码。

在线角色不得读取reward、gold、私有TaskFacts、离线Judge、deterministic或report。
原始session可以持久保存供离线追溯，但不得回灌为后续Executor/Auditor的原始历史。
“丢弃历史”指模型上下文隔离，不是删除实验日志。

## 顺序与交接

每次只执行一个阶段；完成条件通过后停止，给下一个会话提供实际接口、测试命令和限制。
下列文件名和顺序替代旧版阶段编号，不要继续使用旧提示词中的路径。

| 阶段 | 实施单 | 交付 |
|---|---|---|
| 1 | [运行归档与会话身份](01_RUNTIME_AND_EVENTS.md) | 事件契约、session保存、会话隔离、恢复边界 |
| 2 | [Task State与Contract](02_STATE_AND_CONTRACT.md) | 逐条证据更新、失效规则、Manager输入输出契约 |
| 3 | [Fresh Executor与导出](03_AGENT_ADAPTER_FRESH_EXECUTOR.md) | 独立episode、硬预算、任务级双视角trace |
| 4 | [独立Auditor](04_READ_ONLY_AUDITOR.md) | 公开只读检查、终局后成交审计 |
| 5 | [MEA闭环与恢复](05_MEA_LOOP_AND_RECOVERY.md) | 真实Manager、ask路由、调度与恢复 |
| 6 | [协议验收与实验](06_VALIDATION_AND_EXPERIMENT.md) | 同预算对比、开发集/留出集、成本与配对报告 |
| 可选 | [购买前门禁消融](07_OPTIONAL_PURCHASE_GATE.md) | 在核心版本之上独立开关与实验 |

阶段1-4以fixture/mock验证接口；阶段3-5可运行极少量开发任务验证工程可用性。
阶段6的“验收通过”指机制符合协议，不以某个模型必须买对商品为门槛。

交给第一个执行会话的提示：

```text
请完整阅读 docs/prompts/longhorizon-paper-alignment/README.md 和
docs/prompts/longhorizon-paper-alignment/01_RUNTIME_AND_EVENTS.md。
只执行修订版阶段1，完成接口、代码和测试后停止。
交付实际改动文件、测试结果和供阶段2使用的接口；不要提前接通真实MEA闭环。
```

## 关键语义

- 每轮Executor只接收原任务、相关Task State、contract和引用的audit，环境会话保持连续。
- Auditor从独立只读接口获取证据，可使用已有audit提供的历史事实及可追溯引用。
- 一个contract可以incomplete，但其中已独立核验的事实可以保存；逐条推进，不要求
  所有局部工作一起成功。
- 环境done只说明停止购物；随后仍运行只读最终审计。Harness的任务成功另行判断。
- 未完成的审计不会让环境回滚。恢复前必须核对会话与真实环境，并处理未审计动作。
- 人工授权与模型审计是不同概念；可选purchase gate只是运行许可，不代替用户同意。

## 实验口径

先固定与mea-v3相同的10轮上限做架构对比，并固定环境总步数及角色预算。
论文的25轮、Executor每轮1800秒、Manager/Auditor各300秒作为参考配置；
25轮实验单独报告，不能把增加预算造成的提升全部归因于架构。
若历史角色预算或模型参数无法核实，应列为历史参考，另跑匹配配置的对照。

Final-200已被用于调试，是开发/回归比较集；继续复用它和同一Rubric，
但泛化结论须来自另行冻结、未参与调试的任务。
