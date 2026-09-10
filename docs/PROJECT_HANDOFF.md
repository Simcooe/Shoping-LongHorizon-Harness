# 项目交接：Shopping Long-Horizon Harness

## 1. 整体目标

本项目研究 **Shop 场景下的长程 Agent Harness 设计与迭代**，不是模型训练项目，也不是另建一个 CLI Agent。

任务执行由 DSH profile/plugin 驱动，模型通过 ShopSimulator 完成搜索、浏览、选规格、询问用户和购买/放弃。优化对象是 Harness 的状态管理、任务分解、证据利用、审计和终止控制，而不是模型权重。

完整闭环：

```text
DSH 执行任务
  → 原始 session / 工具事件落盘
  → 导出 model_trace + raw_trace
  → 冻结 Rubric + 确定性评测 + 离线 Judge
  → 分析具体失败案例
  → 改进 Harness/profile
  → 在相同任务与评测口径下重新执行并比较
```

研究问题：在尽量固定模型、任务集合和环境的情况下，不同 Harness 是否能降低长程交互中的需求遗忘、错误规格选择、无证据决策、重复操作和终止失控？提升是否值得额外模型调用成本？

评测参考：

- https://github.com/YYHDBL/shopping-grpo-longhorizon
- 本地参考文章：`reference/yyhdbl-shopping-agent-rl.html`

参考项目主要比较 Base/SFT/GRPO；本项目主要比较 h0/h1/MEA profiles。借鉴其 Query + TaskFacts 生成冻结 Rubric、规则与 LLM 分工、四面板而非单一总分的思路，但不照搬训练流程。

## 2. Harness 架构与进度

核心代码：

| 文件/目录 | 职责 |
|---|---|
| `src/shop-tools.js` | DSH 购物工具、环境调用、模型可见结果与元数据 |
| `src/buy-guard.js` | 购买前置条件守卫 |
| `src/mea-loop.js` | MEA 状态、轮次、Manager/Auditor 调用与持久化 |
| `harness/` | h0/h1/mea-v1/mea-v2/mea-v3 等实验 profile |
| `scripts/export_trace.py` | session 导出双视角 trace |
| `scripts/run_benchmark.py` | 组织批次执行和归档；不替代 DSH Agent runtime |
| `environments/ShopSimulator/` | 购物环境 |

演进：

- **h0**：基础购物工具执行。
- **h1**：购买前置门禁。
- **mea-v1**：外部 Task State、固定阶段、round report、公开工具观察。
- **mea-v2**：独立的 Manager LLM，根据状态动态规划下一轮；只读 Executor 工具 schema。
- **mea-v3**：Executor report → Auditor 核验 → Manager 再规划 → 下一轮。

MEA-v3 已实现并跑过真实小批次。当前没有同任务集、同条件的 MEA-v3 全量200条结果可用于正式对比。

重要边界：

- Manager/Auditor 是插件发起的辅助模型调用，不是独立执行服务。
- 在线 MEA 不应读取离线 Rubric、gold/reward 或私有 TaskFacts。
- Manager/Auditor 的判断是被评测对象，不是购物 ground truth。
- 当前部分 round 工具预算/建议工具属于软约束，不能宣称全部具备 runtime 硬拦截。
- mea-v3 profile 不应被误认为自动组合了 buy-guard，需以实际配置为准。
- 有外置 State 不等于已实现 Executor 完整历史上下文裁剪或断点恢复。

## 3. 数据整理后的现状

用户要求：评测目录按 Harness 直接组织，不增加 `shopping-final-v1/legacy/old/refactor` 等中间层。

```text
evaluations/h0/
├── README.md
├── manifest.json
├── traces/
│   ├── 200 个 *.model_trace.json
│   └── 200 个 *.raw_trace.json
├── rubrics/
├── rubric_audit/
└── deterministic/
```

正式 Judge 结果目标目录是 `evaluations/h0/judgments/`，是否已经开始/完成全量运行，需要接手时检查文件及进程，不能依据本交接推定。

原始 runs 按用户明确要求只保留：

```text
runs/h0-0905-1446/
```

这是 h0 完整200条执行产物，保留了原始 session 等数据。其他 runs（包括 MEA 小批次）已清理，不能继续引用为当前仍存在的文件。

旧 `evaluations/shopping-final-v1/` 和 `reports/` 产物已移除。部分旧评测代码移至 `eval/archive/`。移动可能影响旧 import、测试或文档引用；归档不等于已完成依赖迁移，不要未经验证声称旧链路仍能运行。

不要再次删除当前新 Rubric、trace、确定性结果或覆盖原始 session。不要使用 `git reset --hard` / 全局 `git clean`：工作区包含未提交的新实现和整理操作。

## 4. Rubric：已生成并冻结

实现：`eval/gen_rubric_v3.py`

产物：`evaluations/h0/rubrics/`，生成审计：`evaluations/h0/rubric_audit/`。

最近检查：

- 200/200 份生成完成，manifest frozen=true，failed_count=0。
- 生成模型：deepseek-v4-pro。
- explicit requirements：660；taskfact requirements：784；固定过程要求：每任务9条。
- 基础校验通过；这不等于全部语义已经人工证明正确。

输入：

```text
benchmarks/shopping-final-v1/tasks.jsonl
benchmarks/shopping-final-v1/source_goals.private.jsonl
```

TaskFacts 不是一个字面名为 taskfacts 的统一字段。实际需读取每条记录的 `goal.category/attributes/goal_options/expected_brand/expected_model/expected_core_functions/required_options_by_key/price_upper`，以及 instruction_simple/instruction_full。派生字段主要在 `goal` 内，不要照抄早期对话中错误的顶层路径。

冻结内容区分：

- `explicit_requirements`：初始 Query 明确表达。
- `taskfact_requirements`：完整任务目标参考，不默认等于用户初始已知要求。
- `process_requirements`：统一购物过程要求。

Rubric 与具体轨迹无关。虽然物理存于 h0 下，逻辑上所有 Harness 必须复用同一份；不得为 mea-v3 重新随机生成另一套。

用户明确不希望在生成阶段设计复杂生命周期。需求变化由后续 Judge 根据真实 `ask_shopper` 回复解释，不回写冻结 Rubric。实际工具名为 `ask_shopper`，文档中 `shop_asker/shopask` 指同一用户询问功能。

## 5. 确定性评测：已实现并运行 h0 200条

实现与测试：

```text
eval/deterministic_v3.py
eval/tests/test_deterministic_v3.py
```

输出：

```text
evaluations/h0/deterministic/
├── task_results.jsonl
├── summary.json
├── failure_breakdown.json
└── README.md
```

原则：

- 只读取 manifest 和 model/raw trace，不调用 LLM/环境。
- manifest 用于任务集合和元数据，不当 outcome 证据。
- raw steps 用于环境结果、progress 和动作前状态重放。
- model/raw 按位置及工具字段校验对齐；原 step 不保证全局唯一。
- 合法性推断明确标 `inferred_*`，缺失证据不可当作合法/非法的确定结论。
- 工具次数不是 token、延迟或费用。

### 终局口径特别说明

这200条历史 trace 没有显式 `terminal_protocol` 字段。新 evaluator **主动采用** terminal-protocol-v2：第一条 raw.done=true 锁定主结果。它是重评口径，不能倒推历史运行时已实现终局锁。

缺协议声明是 warning，不要补写或修改冻结 trace。`model_trace.terminal` 可能是旧导出摘要，和第一终局不同，不能覆盖 raw steps 的选择。

最近产物记录：

| 指标 | 数值 |
|---|---:|
| 输入完整性通过 | 200/200 |
| 环境终局 | 176/200 |
| 环境任务成功（gold + valid alternative） | 60/200 |
| gold | 59/200 |
| 有购买回执 | 109/200 |
| repeat_loop | 50 |
| max_steps | 17 |
| no_terminal_observed | 24 |
| partial_alternative_purchase | 39 |
| wrong_purchase | 1 |
| reward_unverifiable | 9 |
| 终局后购物工具事件 | 34任务、179次 |

这些只是当前环境分类，不能把 wrong_purchase 很少解释成“真正用户需求违例很少”。商品要求仍需 Judge 分析。正式使用数值前以本地 summary 为准。

## 6. Judge：实现及真实冒烟完成，全量是下一步

实现与测试：

```text
eval/trajectory_judge_v3.py
eval/tests/test_trajectory_judge_v3.py
```

最近单测：42个通过。

真实冒烟目录：

```text
/tmp/judge-smoke/
/tmp/judge-smoke-v2/
/tmp/judge-smoke-v3/
```

最后一轮 `/tmp/judge-smoke-v3/`：任务25、47、108、133、263、596、602；7/7成功，0失败，模型 deepseek-v4-flash。这些是临时目录，可能被系统清理，不作为永久实验档案。

### 输入边界

提供给 LLM 的内容：

- 冻结 Rubric 与 Query。
- model_trace 工具调用、参数、Observation、顺序。
- raw.progress 白名单：consecutive_repeats、no_progress_steps。
- raw.done 派生的中性 runtime_boundary。

不提供 reward、reward_detail、gold reference、私有 goal、完整 observation_state、购买成功标签、确定性 outcome 或 MEA 审计结论。

**不能原样传整个 progress**：其中其他字段可能含 category/budget pass 等内部约束诊断。

### 已修复

- 初始需求依据可引用 rubric，而非伪装成第一个 tool_args。
- shopper 激活依据指向实际用户回复。
- 分开 `user_requirement_verdict` 与 `evidence_status`，latent 不直接计为用户要求 satisfied。
- 分开决策 observed 与 requirements_resolved。
- runtime_boundary 已在输入中存在，后来补充写入输出；并非此前 LLM 完全看不到边界。
- manifest/summary 按本次请求任务处理，包含复用结果的行为需以当前代码测试为准。

### 不应忽略的审查限制

7条成功与42个单测只证明有限范围内链路可用，不代表 Judge 已达到可靠人工水平：

- task133 的最后一次冒烟把 environment_terminal 与 requirements_resolved=true 同时输出，需要检查是否将候选匹配误当最终完成。
- 需求最终满足与“曾见过符合条件的候选”应保持区别。
- task263 的数量判定在不同冒烟中出现过 violated/unknown 波动，模糊预算也需留意。
- 原始 step 可能重复，引用与时间边界不能只依赖 step 数字比较。
- 输出 schema 改动后的缓存应校验版本/输入/Prompt，不能无条件复用旧结果。

这些是后续质检重点，不应通过修改冻结 Rubric 去迎合 Judge。

## 7. 真实终局案例与解释纠正

case602：

```text
Step12 search → no_progress_steps=2, done=false
Step13 back to search → no_progress_steps=3, done=false
Step14 search → no_progress_steps=4, done=true, repeat_loop
Step15 search → 终局后的工具事件
```

Step14 是触发终局的动作，Step15 才是 post-terminal。

case108：Step20首次done，Step21–23继续search，模型可见结果均为 Episode finished、search=False、actions=[]。

case133：Step35返回 max_steps，Step36 finish 返回 early_abstain。重评按第一终局取max_steps。

**纠正早期对话中的过强判断**：这些 trace 不能全部解释成“环境已硬锁且只是回同一个快照”。108终局后的 progress 计数继续增加，133终局原因发生变化，说明历史运行时并非所有内部状态都冻结。相同 Episode finished 文本不证明内部无变化。

也不能仅凭导出工具轨迹判定后续调用一定是新一轮 LLM 生成，还是同批队列动作；需要原始 session 关联。优先称为 post-terminal tool event/protocol error，不把“terminal-state hallucination”当成已证明根因。

## 8. 下一步执行顺序

1. 接手后检查实际 Git 状态、现有 Judge 文件与正在运行的进程，避免重复启动全量。
2. 固定本次 Rubric、Judge Prompt、模型配置与原始 trace。
3. 如尚未全量运行，执行 h0 200条 Judge；不得先删除已有 judgments，使用现有断点续跑语义，先确认缓存兼容。
4. 完成后检查 task集合、成功/失败/复用结果、证据引用、unknown分布和代表性语义案例。
5. 再实现统一报告：环境结果、用户需求满足、过程质量、确定性行为四部分独立展示，不加权成单一总分。
6. h0 基线闭环完成后，再用相同任务和 Rubric 跑 mea-v3，并做逐任务配对比较。
7. 根据评测定位具体 failure，再修改 Harness；不要同时改环境、评测口径和 Harness 导致无法归因。

全量 Judge 示例（这是离线评测，不是新的任务执行入口）：

```bash
python3 -u eval/trajectory_judge_v3.py \
  --input evaluations/h0 \
  --out evaluations/h0/judgments \
  --judge-mode llm \
  --concurrency 4 \
  2>&1 | tee /tmp/h0-judge-run.log
```

请先用 `--help` 和源码确认 resume/retry 行为。不要执行 `rm -rf evaluations/h0/judgments`。

## 9. 相关执行文档

- `docs/prompts/SHOP_RUBRIC_GENERATION_PLAN.md`
- `docs/prompts/DETERMINISTIC_EVALUATION_IMPLEMENTATION.md`
- `docs/prompts/JUDGE_IMPLEMENTATION.md`
- `docs/prompts/FIX_TRAJECTORY_JUDGE_SMOKE_ISSUES.md`
- `docs/prompts/MEA_LOOP_PLUGIN_DESIGN.md`
- `docs/prompts/MEA_MANAGER_V2_IMPLEMENTATION.md`
- `docs/prompts/MEA_AUDITOR_V3_IMPLEMENTATION.md`

旧方案和对话示例可能含不精确字段、语义或示例数字；当前代码、真实输入和明确评测协议优先。发现冲突应说明，不要盲目复制。

## 10. 交接原则

- 用户希望一步步完成，避免反复增加无关字段、目录和服务。
- 要求“写文档”时实际写入项目并给出路径，不只在回复里贴内容。
- 真实模型调用耗时，测试阶段仅跑小规模冒烟；全量评测与代码修改分开。
- 不声称未执行的测试通过，不因存在结果文件就认定语义正确。
- 保留原始证据、不根据评测结果修改Rubric、不混用不同Harness的评分标准。

最终要交付的不是一个购物Demo，而是一套能证明长程Harness机制收益与局限的可复现实验闭环。
