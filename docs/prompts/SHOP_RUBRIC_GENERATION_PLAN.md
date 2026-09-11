# Shop 场景冻结 Rubric 生产方案

> 本文档用于指导 coding agent 实现 Rubric 生成器。
> 当前阶段只实现 **Rubric 生产与冻结**，不实现 Judge，不重构 `evaluate.py`，不修改 DSH 执行逻辑。

---

## 1. 项目背景

当前项目不是训练模型，而是研究：

```text
固定模型
  + 不同 DSH Harness/Profile
  → ShopSimulator 长程购物任务表现
```

任务执行入口始终是 DSH：

```text
DSH Profile / Plugin
  → ShopSimulator
  → 落盘 session
  → 导出 model_trace / raw_trace
  → 离线评测
  → 修改 Harness
```

当前已保存的 h0 评测输入位于：

```text
evaluations/h0/
├── README.md
├── manifest.json
└── traces/
    ├── *.model_trace.json
    └── *.raw_trace.json
```

本次任务只负责生成：

```text
evaluations/h0/rubrics/
```

后续 h0、h1、mea-v1、mea-v2、mea-v3 应复用同一份冻结 Rubric。

---

## 2. 本阶段目标

为 200 个任务生成一份稳定、可复用、与具体 Agent 轨迹无关的 Rubric。

数据流：

```text
用户需求 Query
+
TaskFacts
  ↓
代码提取候选字段
  ↓
LLM Rubric Curator
  ↓
Schema 校验
  ↓
内容去重与一致性校验
  ↓
泄漏检查
  ↓
冻结 Rubric
```

输出：

```text
evaluations/h0/rubrics/<task_id>.json
evaluations/h0/rubrics/manifest.json
```

本阶段禁止：

```text
修改 model_trace/raw_trace
修改 DSH 代码
修改 MEA plugin
实现 Judge
实现动态 requirement timeline
修改 evaluate.py
重新运行 DSH 任务
```

---

## 3. Rubric 的生命周期原则

Rubric 只生成一次，生成后冻结：

```json
{
  "frozen": true
}
```

后续任何 Harness 版本都必须复用同一份 Rubric：

```text
h0
h1
mea-v1
mea-v2
mea-v3
```

Rubric 不得因为：

```text
模型不同
Harness 不同
Agent 轨迹不同
Judge 结果不同
```

而重新生成。

Rubric 生成阶段不读取：

```text
model_trace
raw_trace
旧 Judge 结果
旧 deterministic evaluator 结果
MEA runtime logs
```

这些数据属于后续评测阶段。

---

## 4. TaskFacts 的来源和字段映射

TaskFacts 来源：

```text
benchmarks/shopping-final-v1/source_goals.private.jsonl
```

应使用的字段：

| 语义 | 字段 |
|---|---|
| 用户简短需求 | `instruction_simple` |
| 用户完整需求 | `instruction_full` |
| 商品类别 | `goal.category` |
| 目标商品属性 | `goal.attributes` |
| 目标规格候选 | `goal.goal_options` |
| 核心功能 | `expected_core_functions` |
| 结构化规格 | `required_options_by_key` |
| 显式品牌候选 | `expected_brand` |
| 显式型号候选 | `expected_model` |
| 目标价格上限 | `goal.price_upper` |
| 商品 ID | `asin` / `goal.asin` |
| 用户画像 | `user_persona` / `goal.user_persona` |

不应把以下字段生成成 Rubric requirement：

```text
asin
source_product_index
reason_key
reward_feature_version
option_axis_version
feature_sources
weight
```

这些字段是环境元数据、生成元数据或商品识别信息，不是用户要求。

---

## 5. Query 的来源

公开 Query 主来源：

```text
benchmarks/shopping-final-v1/tasks.jsonl
```

优先使用：

```text
tasks.jsonl.query
```

并校验：

```text
tasks.jsonl.query
==
source_goals.private.jsonl.instruction_simple
```

如果二者不一致，生成器必须报错，不得静默选择其中一个。

`instruction_full` 可以作为 Rubric Generator 的完整需求参考。

---

## 6. Rubric 的核心思想

Rubric Generator 使用：

```text
Query + TaskFacts
```

但输出时必须区分：

```text
1. 用户明确要求
2. TaskFacts 中的完整任务要求
3. 过程行为要求
```

Rubric 不预判：

```text
哪条 TaskFacts 要求未来可以被 shopper 修改
哪条 TaskFacts 要求一定不能被修改
```

因为这一点应由后续 Judge 根据实际 `shop_asker` 对话判断。

Rubric 只负责保存：

```text
这个任务涉及哪些要求
```

而不是保存某次轨迹下的最终有效需求。

---

## 7. Rubric 输出结构

每个任务输出：

```text
evaluations/h0/rubrics/<task_id>.json
```

推荐结构：

```json
{
  "task_id": "263",
  "rubric_version": "shopping-rubric-v2",
  "frozen": true,
  "query": "预算在两百块左右，帮我找一百根玻璃纤维杆子。",
  "explicit_requirements": [
    {
      "id": "r0001",
      "description": "商品品类为玻璃纤维杆子",
      "source": "initial_query",
      "status": "explicit",
      "hardness": "hard",
      "query_quote": "玻璃纤维杆子",
      "taskfacts_basis": ["goal.category"]
    }
  ],
  "taskfact_requirements": [
    {
      "id": "t0001",
      "description": "商品规格应满足任务目标对应的包装和数量要求",
      "source": "taskfacts",
      "status": "taskfact",
      "hardness": "reference",
      "query_quote": null,
      "taskfacts_basis": ["goal.goal_options", "required_options_by_key"]
    }
  ],
  "process_requirements": [
    {
      "id": "p0001",
      "description": "购买前应核验商品类别、关键规格、数量和价格。"
    },
    {
      "id": "p0002",
      "description": "用户通过 shop_asker 明确修改要求后，后续行为应依据最新回复。"
    }
  ],
  "generation_metadata": {
    "source": "query_plus_taskfacts",
    "trajectory_independent": true,
    "shared_across_harness_profiles": true
  }
}
```

必须保留以下语义：

```text
explicit_requirements
taskfact_requirements
process_requirements
source
status
hardness
query_quote
taskfacts_basis
frozen
```

---

## 8. Explicit Requirements 规则

`explicit_requirements` 只包含用户在初始 Query 中表达的要求。

常见要求：

```text
商品类别
品牌
型号
颜色
材质
尺寸
数量
价格
功能
使用场景
适用人群
```

通常判为 `hard`：

```text
商品类别
明确品牌
明确型号
明确颜色
明确尺寸
明确数量
明确功能
“不超过”
“以内”
“以下”
“上限”
```

通常判为 `soft`：

```text
最好
尽量
希望
优先
左右
大约
大概
差不多
```

每条 Query requirement 的 `query_quote` 必须是 Query 中逐字出现的片段。

---

## 9. TaskFacts Requirements 规则

`taskfact_requirements` 用于保存 TaskFacts 中属于完整任务目标的要求，可能包括：

```text
完整规格
包装规格
隐藏数量要求
目标功能
目标材质
目标使用场景
目标品牌或型号
```

但必须注意：

```text
这些要求不代表 Agent 在任务开始时已经知道。
```

TaskFacts requirement 的 `status` 固定为：

```text
taskfact
```

如果该要求在 Query 中没有明确出现：

```text
query_quote = null
```

如果 Query 和 TaskFacts 表达的是同一要求，不要重复生成，统一放入：

```text
explicit_requirements
```

并将 source 标记为：

```text
query_and_taskfacts
```

---

## 10. User Persona 规则

`user_persona` 只作为上下文参考。

例如画像有：

```json
{
  "品牌偏好": [{"品牌名称": "雀巢", "偏好程度": "高"}]
}
```

不能自动生成：

```text
品牌必须是雀巢
```

除非该品牌也出现在 Query 或明确任务要求中。否则不要把 persona preference 生成成 hard requirement。

---

## 11. Gold ASIN 规则

`asin` 不得成为 Rubric requirement。

禁止输出：

```text
必须购买 ASIN 123456789
目标商品 ID 为 123456789
```

Gold ASIN 只能用于：

```text
生成阶段内部对齐
泄漏扫描
质量审计
```

安全 Rubric 中不得出现 Gold ASIN。

---

## 12. Process Requirements

每个任务可以使用统一的过程要求，不需要 LLM 每次自由创造。

建议固定包括：

```text
购买前核验商品类别
购买前核验关键规格
购买前核验数量
购买前核验最终价格
不能仅凭标题推断详情
必要时进行 shopper 澄清
用户修改要求后使用最新回复
自然语言声明不能替代环境证据
终局后不能继续行动
```

这些要求用于后续 Judge 评价：

```text
搜索策略
候选利用
证据核验
需求保持
决策质量
终止效率
```

本阶段不实现 Judge。

---

## 13. Shopper 需求变化的处理原则

Rubric 生成时：

```text
不要预判需求是否可修改
不要生成某条需求的固定生命周期
不要根据历史轨迹修改 Rubric
```

冻结 Rubric 只保存：

```text
任务可能涉及的要求集合
```

后续 Judge 读取：

```text
冻结 Rubric
+
实际 model_trace/raw_trace 中的 shop_asker 问答
```

再判断用户是否确认、修改、放宽、拒绝某条要求。

本阶段只需在 Rubric 中保留：

```json
{
  "lifecycle_policy": {
    "shopper_changes_are_judge_time_interpretation": true,
    "rubric_is_not_modified_by_trajectory": true
  }
}
```

---

## 14. Generator 的推荐输入

不要把完整私有 JSON 原样传给 LLM。先由代码构造精简候选：

```json
{
  "task_id": "602",
  "query": "麻烦推荐一款用来提神的咖啡液，我的预算在40元左右。",
  "instruction_full": "...",
  "taskfacts_candidates": {
    "category": "咖啡液",
    "attributes": ["浓缩", "冷热", "提神"],
    "core_functions": ["提神"],
    "options": [{"key": "flavor", "value": "焦糖玛奇朵1盒+生椰拿铁1盒"}],
    "brand_candidates": [],
    "model_candidates": [],
    "price_upper": 44.0
  }
}
```

不要把以下内容传给 Curator：

```text
完整 user_persona
reward
reason_key
feature_sources
source_product_index
原始内部元数据
```

`asin` 可以保留在代码侧用于泄漏检查，但不要传给 LLM。

---

## 15. Rubric Curator Prompt 要求

Generator Prompt 必须说明：

```text
你是 ShopSimulator 长程购物任务的 Rubric Curator。

输入包括：
1. 用户初始公开需求 query；
2. 用户完整需求 instruction_full；
3. 代码从 TaskFacts 提取的候选字段。

请生成一份与具体 Agent 轨迹无关、生成后冻结、并由所有 Harness/Profile 复用的任务 Rubric。

请输出三类内容：
1. explicit_requirements：Query 中明确表达的要求；
2. taskfact_requirements：TaskFacts 中属于完整任务目标、但 Query 未必完整表达的要求；
3. process_requirements：长程购物中的搜索、核验、澄清、决策和终止要求。

必须遵守：
- Query 是用户显式表达要求的最高来源。
- TaskFacts 可以补充完整任务目标，但不能改变 Query 的含义。
- Query 和 TaskFacts 表达同一要求时只保留一条。
- asin、reward、reason_key、source_product_index 等内部字段不能成为 requirement。
- user_persona 偏好不能自动变成 hard requirement。
- 不根据任何 Agent trajectory 生成 Rubric。
- 不为 requirements 预先决定是否可以被 shopper 修改。
- shopper 修改由后续 Judge 根据真实轨迹判断。
- “不超过、以内、以下、上限”通常是 hard。
- “左右、大约、大概、最好、尽量、希望”通常是 soft。
- 不重复拆分同一语义要求。
- 只输出 JSON，不要解释。
```

---

## 16. 代码提取候选与 LLM 归并

推荐流程：

```text
source_goals.private.jsonl
  ↓
代码提取 category / attributes / options / functions / price 等候选
  ↓
去除 ASIN、reward、内部元数据
  ↓
传给 LLM Curator
  ↓
LLM 负责选择、去重、归并、自然语言抽象、hardness 标注
```

LLM 不应自由创造 TaskFacts 中没有依据的具体值。

如果 TaskFacts 中存在重复信息，例如：

```text
goal.attributes = ["低糖", "海鲜", "凉菜", "拌面"]
expected_core_functions = ["低糖", "多用途", "提鲜"]
```

应归并为少量语义清晰的 requirement，而不是机械生成 7 条重复约束。

---

## 17. 生成后的校验

每个 Rubric 必须通过以下校验：

### 17.1 基础 Schema

```text
task_id 正确
rubric_version 正确
frozen == true
requirements 是数组
id 唯一
source 合法
status 合法
hardness 合法
```

### 17.2 Query Quote

对 `source = initial_query` 或 `source = query_and_taskfacts`：

```text
query_quote 非空
query_quote 是 query 的逐字子串
```

### 17.3 TaskFacts Basis

对 `source = taskfacts` 或 `source = query_and_taskfacts`：

```text
taskfacts_basis 非空
字段路径合法
```

### 17.4 去重

检查：

```text
description 不重复
Query 和 TaskFacts 同义要求不重复
同一价格要求不重复
同一规格要求不重复
```

### 17.5 泄漏

安全 Rubric 不得包含：

```text
gold ASIN
source_product_index
reason_key
reward
reward_type
完整 hidden goal JSON
完整 user_persona
```

允许 Query 中已经出现的品牌、型号、规格、价格、属性。

---

## 18. Rubric Audit

建议生成：

```text
evaluations/h0/rubric_audit/<task_id>.json
```

该目录不提供给 Judge。

内容示例：

```json
{
  "task_id": "263",
  "query_requirement_count": 3,
  "taskfact_requirement_count": 2,
  "process_requirement_count": 5,
  "gold_asin_present_in_safe_rubric": false,
  "hidden_leak_hits": [],
  "duplicate_requirements": [],
  "query_quote_errors": [],
  "taskfacts_basis_errors": [],
  "validation_errors": []
}
```

---

## 19. Rubric Manifest

生成：

```text
evaluations/h0/rubrics/manifest.json
```

至少包含：

```json
{
  "rubric_version": "shopping-rubric-v2",
  "frozen": true,
  "source_policy": "query_plus_taskfacts",
  "trajectory_independent": true,
  "shared_across_harness_profiles": true,
  "task_count": 200,
  "generated_count": 200,
  "failed_count": 0,
  "failed_tasks": [],
  "generator_version": "shopping-rubric-generator-v1",
  "tasks_sha256": "...",
  "taskfacts_sha256": "..."
}
```

如果生成失败，不得将 manifest 标记为 `frozen=true`。

---

## 20. 人工抽查任务

批量生成前先检查：

```text
263
596
602
```

覆盖：

```text
数量 + 模糊价格
颜色 + 使用场景 + 价格
功能 + hidden 规格 + 多口味
```

重点检查：

```text
Query 明确要求是否被保留
TaskFacts 要求是否被合理归并
TaskFacts 是否全部错误地变成 hard
用户画像偏好是否被错误升级
ASIN 是否泄漏
重复要求是否重复生成
Query quote 是否逐字正确
```

确认样例通过后，再批量生成 200 条。

---

## 21. 验收标准

```text
[ ] 输出目录为 evaluations/h0/rubrics/
[ ] 生成 200 个 task Rubric
[ ] task_id 与 evaluations/h0/manifest.json 对齐
[ ] Query 与 TaskFacts 输入对齐
[ ] 每份 Rubric frozen=true
[ ] Query 明确要求没有遗漏
[ ] TaskFacts 参与了 Rubric 生成
[ ] TaskFacts 需求与 Query 需求没有重复
[ ] shopper 生命周期没有被提前写死
[ ] 不包含 gold ASIN
[ ] 不包含 reward / reason_key / 内部环境元数据
[ ] 不依赖 model_trace/raw_trace
[ ] 不依赖任何旧 Judge 结果
[ ] 不依赖任何 Harness profile
[ ] 所有 Harness 版本可以复用
[ ] 263、596、602 已人工抽查
[ ] rubric manifest 记录输入 hash
[ ] 生成失败时不会伪造 frozen=true
```

---

## 22. 本阶段明确不做的内容

不要实现：

```text
Judge
Trajectory Judge
effective requirements
requirement timeline
shopper reply 解析
deterministic evaluator
MEA protocol evaluator
DSH 执行入口
新的环境服务
新的购买逻辑
```

本阶段只负责：

```text
Query + TaskFacts
→ frozen rubric
```

---

## 23. 最终数据流

```text
benchmarks/shopping-final-v1/tasks.jsonl
  +
benchmarks/shopping-final-v1/source_goals.private.jsonl
        ↓
代码提取 TaskFacts 候选
        ↓
Rubric Curator LLM
        ↓
evaluations/h0/rubrics/<task_id>.json
        ↓
schema / quote / duplicate / leakage 校验
        ↓
evaluations/h0/rubrics/manifest.json
        ↓
frozen=true
```

后续评测阶段才是：

```text
frozen rubric
+
DSH 产生的 model_trace/raw_trace
        ↓
Judge
        ↓
判断用户需求、shop_asker 变化和过程质量
```

---

## 最终要求

另一个 coding agent 仅完成：

```text
生成并冻结 200 份 Rubric
```

Rubric 的核心来源是：

```text
Query + TaskFacts
```

Rubric 必须跨 Harness 版本复用。

不要在本阶段处理：

```text
shop_asker 需求生命周期
Judge
evaluate
MEA runtime logs
```

这些属于后续阶段。
