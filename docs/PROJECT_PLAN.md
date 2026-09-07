# Shopping Long-Horizon Harness 项目方案

> 当前分支：`main_evaluate_pipeline`  
> 项目目标：面向 **Multi-Turn + Personalization** 购物场景，构建一个可审计、可恢复、可评测的长程 Agent Harness，用于秋招项目展示。

## 1. 项目定位

本项目不是单纯实现一个购物 Agent，也不是只增加一个购买规则，而是研究和实现：

> 如何让购物 Agent 在需求不完整、需要多轮澄清、候选商品较多、规格和预算约束复杂的长程任务中，保持状态一致，并可靠地完成购买决策。

主要参考 `reference/LongHorizon-Harness.pdf` 的思想，将其 Manage–Execute–Audit（MEA）架构迁移到 ShopSimulator：

```text
用户初始需求 / 用户画像
        ↓
多轮需求澄清
        ↓
显式任务状态 Task State
        ↓
Manager：选择下一项 bounded subtask
        ↓
Executor：执行当前子任务
        ↓
Auditor：独立验证环境状态
        ↓
更新已验证状态 / 失败恢复
        ↓
继续下一轮，或完成购买
```

## 2. 当前已有基础

当前仓库已经具备：

- ShopSimulator 环境快照；
- `search`、`click`、`finish` 工具；
- `ask_shopper` 多轮用户询问工具；
- Shopper Simulator；
- H0 baseline profile；
- H1 Buy Guard profile；
- `model_trace` / `raw_trace` 双视角轨迹；
- 多批 baseline 运行数据；
- 基于失败轨迹的 failure analysis；
- 第一版 Rubric Builder、LLM Judge 和四面板报告，原先位于旧项目的 `self-harness-dsh/eval`，后续需要迁移和适配到当前仓库。

### 当前 Buy Guard

Buy Guard 是第一阶段的 action-level safety mechanism。它在 `click[buy now]` 执行前检查：

1. 当前是否处于 `product_detail`；
2. 当前可点击按钮中是否确实存在 `buy now`；
3. 是否已经选择商品规格。

它可以解决“模型在 information subpage 中点击 Buy Now、环境静默忽略、模型误以为购买成功”的典型问题。

但 Buy Guard 只保护单个动作，不能解决：

- 用户需求丢失；
- 候选商品误判；
- 价格和预算核验；
- 多规格完整选择；
- 多轮用户需求修改；
- 重复搜索和长程循环；
- 购买后错误完成判断。

因此后续要从 action-level guard 发展为 state-level、transaction-level 和 process-level harness。

## 3. 固定实验场景：Multi-Turn + Personalization

正式 Benchmark 的 200 条任务全部使用 Multi-Turn + Personalization，不混入普通单轮任务。

候选条件：

```text
product.tag == "eval"
user_persona 非空
instruction_simple 非空
instruction_simple != instruction_full
```

当前数据统计：

- eval 商品约 1459 条；
- eval 且有非空 `user_persona` 约 1343 条；
- 有 `instruction_simple` 约 1343 条；
- 可形成信息差的任务足够选出 200 条。

正式运行固定：

```bash
SHOPSIM_IF_PERSONA=1
SHOPPER_BASE_URL=http://127.0.0.1:5701
```

运行语义为：

```text
初始 instruction_simple / 可见任务信息
        ↓
Agent 判断是否需要澄清
        ↓
调用 ask_shopper
        ↓
Shopper Simulator 逐步返回信息
        ↓
Agent 维护已知需求并继续购物
```

需要重点观察：

- Agent 是否识别需求不完整；
- 是否提出有效的澄清问题；
- 是否重复询问已知信息；
- 是否记住 Shopper 的回复；
- 是否处理用户拒绝；
- 是否处理用户需求修改；
- 是否将澄清后的需求用于候选筛选；
- 是否在完整需求满足后购买。

## 4. Benchmark 设计

### 4.1 固定 200 条任务

从 ShopSimulator 全量商品和 goals 中生成候选，然后筛选 `tag == eval` 且满足 Multi-Turn + Personalization 条件的任务。

重要：ShopSimulator 通过 `reset(idx)` 使用全量 `server.goals` 的索引，因此：

> `task_id` 必须是全量 goals 中的 global goal index，不能对 eval 商品重新编号。

正确流程：

```python
all_products = load_products(...)
all_goals = get_goals(all_products, ...)
assert len(all_products) == len(all_goals)

for global_idx, (product, goal) in enumerate(zip(all_products, all_goals)):
    assert str(product["asin"]) == str(goal["asin"])
    if product["tag"] == "eval" and ...:
        # 保留 global_idx 作为 task_id
```

不能使用：

```text
先筛 eval 商品 → 再调用 get_goals → 重新编号
```

### 4.2 Benchmark 文件

计划建立：

```text
benchmarks/
└── shopping-final-v1/
    ├── manifest.json
    ├── tasks.jsonl
    └── source_goals.private.jsonl
```

- `manifest.json`：固定 task id、数据版本、环境版本、persona 模式和采样信息；
- `tasks.jsonl`：公开任务与非敏感 metadata；
- `source_goals.private.jsonl`：完整 goal、ASIN 和隐藏事实，仅用于离线 Rubric 生成与结果分析。

`source_goals.private.jsonl` 必须加入 `.gitignore`，不能提供给 Agent 或 Trajectory Judge。

### 4.3 公开任务不能泄漏答案

公开 `tasks.jsonl` 不得包含：

- `asin` / `gold_asin`；
- 完整 `goal`；
- `instruction_full`；
- `goal_options`；
- `expected_brand`；
- `expected_model`；
- 完整 `user_persona`；
- `reason_key`；
- hidden TaskFacts。

公开 Query 必须与正式实验中 Agent 实际看到的初始 Query 模式一致，并在 manifest 中记录 Query mode。

### 4.4 采样策略

200 条任务使用固定 seed 做确定性分层采样，优先覆盖：

- 所有 `domain_zh`；
- 有预算 / 无预算；
- 有品牌 / 无品牌；
- 有型号 / 无型号；
- 有规格 / 无规格；
- 不同 persona 丰富度；
- 不同初始 Query 与完整需求的信息差；
- 不同约束数量和复杂度；
- hard constraint 与 soft preference。

最终结果必须：

- 恰好 200 条；
- 无重复；
- task id 升序保存；
- 全部来自 eval；
- 全部有 persona；
- 全部有 instruction_simple 和信息差；
- 在相同 seed 下可复现。

当前 32 条左右的历史任务用于 development / failure analysis，不作为最终 Benchmark。

## 5. 评测体系

评测不能只看 ShopSimulator 的最终 reward。项目采用两路评测方案，最后汇总到同一份多面板报告中：

```text
路线 A：Environment / Benchmark Evaluation
  raw_trace + ShopSimulator 后端结果
  → 评估最终购买结果和环境真值

路线 B：Rubric / Trajectory Evaluation
  model_trace + 冻结 Rubric + LLM Judge
  → 评估用户需求满足度和执行过程质量
```

两路评测的职责不同，不能互相替代：

- 路线 A 是确定性的最终结果评测，回答“最后买得对不对”；
- 路线 B 是过程和需求评测，回答“用户要求是否逐条处理、是否有证据、过程是否合理”；
- 另外通过 Harness telemetry 记录 Guard、State、Recovery、MEA 等模块是否真正生效；
- 不把两路结果粗暴相加成一个总分，而是分别报告后进行联合分析。

最终评测由四个核心面板和一个可选 Harness 内部面板组成：

```text
Panel 1：Environment Outcome
Panel 2：Rubric Satisfaction
Panel 3：Trajectory Quality
Panel 4：Deterministic Behavior
Panel 5：Harness Internals
```

### Panel 1：环境结果

直接使用 `raw_trace` 和 ShopSimulator evaluator 的确定性结果：

- gold purchase rate；
- valid alternative purchase rate；
- wrong purchase rate；
- any purchase rate；
- average reward；
- `reward_type` 分布；
- `termination_reason` 分布；
- `purchase_success`；
- `reward_valid`。

不要把 `purchase_success == true` 直接等同于 gold 任务成功，要明确区分：

```text
gold_purchase
valid_alternative_purchase
wrong_purchase
any_purchase
```

### Panel 2：Rubric 需求满足度

Rubric 从用户可见 Query 和多轮澄清内容中提取用户真正表达的约束，区分：

```text
satisfied
violated
unknown
not_applicable
```

Rubric 不能简单照抄隐藏 TaskFacts，也不能把 gold ASIN 当作用户要求。

每一条 Rubric 应记录：

```json
{
  "id": "c0001",
  "description": "价格不超过 500 元",
  "hardness": "hard",
  "source": "initial_query 或 shopper_clarification",
  "query_quote": "500 元以内"
}
```

每个 H0/H1/H2/H3 都使用同一份冻结 Rubric。

### Panel 3：轨迹过程质量

使用 LLM Judge 评估：

- `search_strategy`：搜索词、收敛性、重复搜索；
- `candidate_utilization`：是否打开、比较和利用候选；
- `evidence_verification`：是否核验商品属性、规格和价格；
- `decision_quality`：购买或放弃是否符合需求；
- `termination_efficiency`：是否过早终止、重复循环或过度搜索。

每项使用 0/1/2 分，并要求引用真实的 model trace step。

Judge 只能看到：

```text
用户 Query
冻结 Rubric
model_trace
模型实际看到的 ask_shopper / guard feedback
```

不能看到：

```text
raw_trace 中的隐藏字段
reward
gold ASIN
TaskFacts
完整隐藏 goal
```

### Panel 4：确定性行为指标

从 trace 通过代码统计：

- 总步数；
- `search` 次数；
- `click` 次数；
- `ask_shopper` 次数；
- invalid click 次数和比例；
- 重复搜索；
- 重复 action；
- no-progress；
- repeat loop；
- Buy Now 尝试次数；
- Guard deny 次数；
- 主动 finish；
- 上下文或基础设施错误；
- 信息泄漏。

### Panel 5：Harness 内部指标

随着 Harness 迭代，增加：

- `guard_loaded`；
- Guard allow / deny 次数；
- deny rule 分布；
- false block rate；
- Task State transition 数；
- verified / pending / suspect 数量；
- audit report 数；
- contract violation 数；
- recovery trigger 数；
- recovery success rate；
- MEA round 数。

这些指标不能被最终 reward 替代，因为它们用于证明 Harness 的机制确实生效。

## 6. Rubric Pipeline

当前仓库中已有第一版 Rubric Pipeline，原结构包括：

```text
gen_goals.py
    ↓
gen_rubric.py
    ↓
rubrics/
    ↓
judge.py
    ↓
judgments/
    ↓
report.py
```

已有设计应保留：

- Query-grounded Rubric；
- hard / soft；
- `query_quote`；
- Rubric 冻结；
- Judge 四态 verdict；
- 五个过程维度；
- step reference；
- 四面板报告；
- 不强行合成单一总分。

迁移到当前项目时需要修正：

1. Rubric Query 必须和 persona 模式下 Agent 实际看到的 Query 对齐；
2. gold / alternative / wrong 成功语义要分开；
3. Judge 必须严格覆盖所有 Rubric；
4. `step_reference` 必须引用真实存在的 step；
5. Observation 截断不能丢失价格、规格、按钮列表和用户回复等关键证据；
6. Guard telemetry 要进入 Harness 面板，不能只从自然语言 grep；
7. judge 输入不能泄漏 raw goal、reward 或 gold ASIN。

## 7. Harness 迭代路线

### H0：Baseline

原始 Agent，不增加新的 Harness 机制。

### H1：Buy Guard

已有第一步：

```text
click[buy now]
        ↓
检查页面状态 / 按钮 / 规格
        ↓
allow 或 deny
```

H1 的主要目标：

- 降低非法购买；
- 降低静默失败；
- 减少错误完成判断；
- 合法购买误拦截接近 0。

### H2：Explicit Task State

维护结构化状态：

```json
{
  "requirements": [],
  "candidates": [],
  "selected_product": null,
  "selected_options": {},
  "known_facts": [],
  "pending_requirements": [],
  "verified_evidence": [],
  "purchase_status": "not_ready",
  "last_checkpoint": null
}
```

状态记录至少包含：

- Requirement；
- Artifact；
- Fact；
- status；
- evidence reference。

状态原则：

> Executor 的自我声明不能直接将状态更新为 verified，只有环境检查或 Auditor 才能推进已验证状态。

### H3：购买事务控制

把购买从普通 click 升级为事务：

```text
SEARCHING
  → CANDIDATE_SELECTED
  → PRODUCT_VERIFIED
  → OPTIONS_VERIFIED
  → PRICE_VERIFIED
  → PURCHASE_ARMED
  → PURCHASE_COMMITTED
  → PURCHASE_AUDITED
```

购买前检查：

- 候选商品明确；
- 商品属性满足硬约束；
- 价格满足预算；
- 所有必选规格已选择；
- 当前页面为商品详情页；
- Buy Now 当前可用；
- 用户确认条件满足。

购买后检查：

- 是否真的发生购买；
- 购买的商品是否是当前候选；
- 规格是否正确；
- 价格是否正确；
- 是否为 gold、valid alternative 或 wrong purchase；
- 是否出现错误完成判断。

### H4：Loop Detection + Recovery

检测：

- 连续重复 action；
- 页面状态 hash 不变；
- 多步没有新增 verified requirement；
- invalid click 比例过高；
- 页面来回震荡；
- 重复搜索。

恢复：

1. 标记当前状态为 suspect；
2. 保存最近 verified checkpoint；
3. 清理过期页面状态；
4. 重新观察当前页面；
5. 生成 recovery contract；
6. 从可信状态继续执行。

恢复不是简单地让模型“再试一次”，而是根据已验证状态重新规划。

### H5：MEA / Fresh-context Execution

实现：

```text
Manager
  → bounded contract
  → fresh-context Executor
  → read-only Auditor
  → verified state transition
```

Manager：

- 选择未完成 requirement；
- 生成当前 subtask contract；
- 指定 acceptance criteria；
- 指定 boundary constraints；
- 决定 execute / done / blocked / ask。

Executor：

- 只获得当前任务、当前状态、当前 contract 和相关证据；
- 不获得全部历史 transcript；
- 只执行当前 bounded subtask。

Auditor：

- 使用只读工具；
- 独立检查环境；
- 不接受 Executor 的完成声明作为证据；
- 生成 completion、integrity、verified facts 和 remaining gaps。

## 8. 实验设计

正式 Benchmark 应固定为：

```text
shopping-final-v1：200 条 Multi-Turn + Personalization 任务
```

所有配置使用同一批：

```text
H0：Final-200
H1：Final-200
H2：Final-200
H3：Final-200
H4：Final-200
```

每次实验固定：

- task_id；
- Query mode；
- Rubric version；
- ShopSimulator environment version；
- model；
- temperature / max tokens；
- max steps；
- Shopper Simulator 配置。

建议：

- 开发阶段：200 条 × 1 trial；
- 最终结果：200 条 × 2～3 trials；
- 所有配置使用相同 task ids 和相同 Rubric。

除了 aggregate metrics，还要做 task-level paired comparison：

```text
improved
unchanged
regressed
```

最终报告不建议把所有指标强行合成一个总分，而是分别展示：

```text
Environment Outcome
Rubric Satisfaction
Trajectory Quality
Harness Reliability
Cost / Efficiency
```

## 9. 工程目录规划

目标目录结构：

```text
benchmarks/
└── shopping-final-v1/
    ├── manifest.json
    ├── tasks.jsonl
    ├── source_goals.private.jsonl
    └── rubrics/

src/
├── shop-tools.js
├── buy-guard.js
├── state/
│   ├── task-state.js
│   ├── state-store.js
│   ├── state-transition.js
│   └── evidence.js
├── contracts/
│   ├── contract-builder.js
│   ├── contract-validator.js
│   └── contract-schema.js
├── audit/
│   ├── product-auditor.js
│   ├── variant-auditor.js
│   ├── budget-auditor.js
│   └── purchase-auditor.js
├── recovery/
│   ├── loop-detector.js
│   └── recovery-policy.js
└── telemetry/
    ├── event-log.js
    ├── metrics.js
    └── trace-schema.js

eval/
├── gen_goals.py
├── gen_rubric.py
├── evaluate.py
├── judge.py
├── report.py
└── compare.py

scripts/
├── freeze_benchmark.py
├── run_benchmark.py
└── export_trace.py
```

## 10. 实施顺序

### 阶段一：冻结 Benchmark

1. 从 eval split 中筛选 200 条 Multi-Turn + Personalization 任务；
2. 保留全量 global goal index；
3. 生成 `manifest.json`、`tasks.jsonl` 和 private source goals；
4. 校验 task id、ASIN 对齐、persona、信息差和可复现性；
5. 冻结 Benchmark。

### 阶段二：迁移并跑通评测

1. 将旧版 `eval/` 迁移到当前仓库；
2. 修正 Query / persona 对齐；
3. 修正 gold / alternative / wrong 结果语义；
4. 生成 200 份冻结 Rubric；
5. 让 H0/H1 都能生成四面板报告；
6. 增加 profile、trial 和 Harness metadata。

### 阶段三：正式运行 H0/H1

1. 实现支持离散 task ids 的有界并发 runner；
2. worker 数不超过 ShopSimulator slot 数；
3. 跑固定 200 条 H0；
4. 跑固定 200 条 H1；
5. 对比环境结果、Rubric 结果和 Harness 指标。

### 阶段四：实现状态与恢复

1. Task State；
2. Requirement / Fact / Evidence；
3. Purchase transaction；
4. Loop Detector；
5. Checkpoint；
6. Recovery；
7. 在同一 Benchmark 上跑 H2/H3。

### 阶段五：实现 MEA

1. bounded subtask contract；
2. contract validator；
3. fresh-context executor；
4. deterministic auditors；
5. manager policy；
6. audit report；
7. 在同一 Benchmark 上跑 H4。

### 阶段六：最终展示

准备：

- H0～H4 ablation 表；
- task-level improved / unchanged / regressed；
- failure mode breakdown；
- Rubric satisfaction breakdown；
- cost-performance 分析；
- 架构图；
- 典型失败与恢复案例；
- README；
- 技术报告；
- Demo 视频。

## 11. 最终项目叙事

推荐在秋招中这样描述：

> 我针对 Multi-Turn + Personalization 购物场景，对 Agent baseline 轨迹进行分析，发现长程失败不仅来自商品理解，也来自需求澄清后的状态丢失、页面状态误判、规格和预算未核验，以及购买动作的错误提交。首先，我在不修改 prompt 的情况下实现了工具层 Buy Guard，拦截不满足页面和规格前置条件的购买动作。随后，我将这个局部机制扩展为显式任务状态、购买事务约束、独立审计和失败恢复组成的可靠性 Harness。为了验证每个模块，我固定了 200 条 Multi-Turn + Personalization Benchmark，并使用环境结果、Query-grounded Rubric、轨迹过程质量和 Harness telemetry 进行分层评测，通过 H0～Hn ablation 分析每个组件的实际贡献。

项目最终定位为：

> **A constraint-aware, auditable and recoverable long-horizon harness for multi-turn personalized shopping agents.**

## 12. 核心原则

1. 先冻结数据，再开发 Harness；
2. 正式 Benchmark 的 task_id、Query、Rubric 和环境版本保持不变；
3. 32 条历史任务只用于开发和 failure analysis；
4. 环境 reward 评估最终结果，Rubric 评估需求和过程；
5. 不把 Reward、Rubric、Harness telemetry 混成一个分数；
6. 不信任模型的自我完成声明；
7. 只有环境证据才能推进 verified state；
8. 购买是高风险事务，需要 precondition 和 postcondition；
9. 失败要被记录并用于恢复，而不是被长上下文掩盖；
10. 每增加一个 Harness 模块，都用 ablation 证明它的作用；
11. 所有正式实验都使用相同的 200 条 Multi-Turn + Personalization 任务；
12. 所有隐藏 TaskFacts、gold ASIN 和 private goals 都不能泄漏给 Agent 或 Judge。
