# Shopping 长程任务 Judge 实现方案

## 目标

实现离线 LLM Judge，读取冻结 Rubric、model_trace，以及 raw_trace 中脱敏后的 `progress`，评价 ShopSimulator 长程购物过程。本阶段不修改 DSH、Harness、Rubric 或 deterministic evaluator。

## 输入边界

输入目录：`evaluations/h0/`，包含：

```text
manifest.json
traces/*.model_trace.json
traces/*.raw_trace.json
rubrics/*.json
```

Judge 允许读取：

- 冻结 Rubric；
- model_trace 的 `step/tool_name/tool_args/observation`；
- raw_trace 每步 `raw.progress.consecutive_repeats` 和 `raw.progress.no_progress_steps`。

Judge 禁止读取或接收：

```text
reward / reward_detail / reward_type
termination_reason
purchase_success
完整 purchase 回执
gold ASIN
完整 hidden TaskFacts
observation_state
source_goals.private.jsonl
旧 judgment / report
MEA runtime logs
```

`progress` 只能作为过程诊断，不能作为商品满足、购买成功或 Gold 命中的证据。

## Rubric 语义

Rubric 已由 Query + TaskFacts 生成并冻结，Judge 不得修改 Rubric 文件。

- `explicit_requirements`：初始 Query 明确要求，从任务开始有效。
- `taskfact_requirements`：完整 TaskFacts 中的候选要求，初始不一定已被用户告知。

Judge 读取实际 model_trace 中的 `ask_shopper` 及用户回复，临时解释本次轨迹中的有效需求：

```text
active / latent / modified / rejected / revoked / unknown
```

这个解释只写入 Judge 输出，不回写冻结 Rubric。没有用户明确确认的 TaskFacts requirement，不能仅因为轨迹未满足就判 `violated`。

## 任务输出

每个 task 输出：

```json
{
  "task_id": "263",
  "judge_version": "trajectory-judge-v3",
  "rubric_version": "shopping-rubric-v2",
  "decision": {
    "resolved": true,
    "step": 15,
    "kind": "purchase|finish|environment_terminal|unresolved"
  },
  "requirement_interpretation": [],
  "rubric_verdicts": [],
  "dimension_scores": {
    "clarification_strategy": 0,
    "information_retention": 0,
    "search_strategy": 0,
    "candidate_utilization": 0,
    "evidence_verification": 0,
    "decision_quality": 0,
    "termination_efficiency": 0
  }
}
```

每条 Rubric requirement 恰好一个 verdict：

```text
satisfied / violated / unknown / not_applicable
```

`effective_status` 与 verdict 分开。`satisfied`/`violated` 必须引用真实 model_trace step；`unknown` 通常无引用。Evidence reference 使用结构化对象：

```json
{"source":"model_trace","step":7,"field":"observation"}
{"source":"model_trace","step":7,"field":"tool_args"}
{"source":"raw_progress","step":8,"field":"no_progress_steps"}
```

不能只输出裸 step 数字。

## 评测规则

- Agent 自己的自然语言声明不是商品事实。
- 搜索标题不能自动证明详情规格满足。
- 缺证据是 `unknown`，不是 `violated`。
- `finish`/`Episode finished` 不能被 Judge 当作购买成功证据。
- 需求修改、确认、放宽、拒绝以用户实际回复为准。
- 只使用决策时刻之前（含决策时刻）的证据；终局后的动作不能证明之前的决策。
- `progress` 只帮助判断循环、无进展和终止效率，必须结合动作与 Observation。

过程维度均为 0/1/2：

1. `clarification_strategy`：是否识别关键缺口并进行必要、具体、不重复的澄清。
2. `information_retention`：是否记住初始需求和最新用户回复，避免继续旧需求。
3. `search_strategy`：搜索词是否覆盖要求，是否根据结果收敛而非重复搜索。
4. `candidate_utilization`：是否打开、比较、筛选已发现候选并推进核验。
5. `evidence_verification`：购买/结束前是否核验类别、规格、数量、价格，而非只看标题。
6. `decision_quality`：最终选择或放弃是否符合有效需求和公开证据。
7. `termination_efficiency`：是否避免过早终止、循环、无效动作和终局后动作。

## LLM System Prompt 必须包含

```text
你只能使用冻结 Rubric、model_trace 和白名单 progress diagnostics。
你不能访问或假设 reward、gold ASIN、hidden TaskFacts、termination_reason、purchase_success、完整 raw state 或其他评测结果。
TaskFacts requirement 是完整任务目标候选；若没有通过真实 shopper 回复确认，不能仅因轨迹未满足就判 violated。
用户通过 ask_shopper 的明确确认、修改、放宽、拒绝优先于初始信息和 TaskFacts 候选。
Executor 的自然语言声明不是事实；只有模型可见 Observation、工具参数和用户回复可作为过程证据。
progress 仅是重复/无进展诊断，不能证明商品满足或购买成功。
每条 satisfied/violated 必须引用真实 model_trace step；缺证据判 unknown；只输出严格 JSON。
```

## 严格校验

代码必须校验：

```text
JSON 合法
任务和 Rubric ID 一致
每个 requirement 恰好出现一次
状态值合法
effective_status 合法
每个维度为 0/1/2
所有引用的 step 存在
reasoning 非空
```

非法输出最多重试一次；仍失败则写 `judge_failed`，不伪造结果。

## 测试策略：先冒烟，不直接跑 200 条

Judge 输入轨迹较长，运行慢且可能调用 LLM 重试。必须按以下顺序：

### 1. Mock/单元测试

不调用真实 LLM，测试输入构造、progress 白名单、Rubric 对齐、JSON 解析、step 引用、shopper 字段处理、非法 JSON 重试和缺失输入。

### 2. 真实 LLM smoke test

只运行 5–8 个代表任务，输出到临时目录，不写正式全量目录。建议包含：

```text
263, 596, 602
一个成功购买任务
一个 repeat_loop 任务
一个 max_steps 任务
一个 no-terminal 任务
一个真实含 ask_shopper 回复的任务
```

命令示例：

```bash
python3 eval/trajectory_judge_v3.py \
  --input evaluations/h0 \
  --out /tmp/judge-smoke \
  --only 263,596,602,47,108,133 \
  --concurrency 2
```

任务不存在时按实际输入替换；`--only` 至少要支持。Smoke 结果不能被当作完整 200-task 结果。

冒烟后人工检查：

```text
TaskFacts 未确认时没有默认 violated
shop_asker 修改被正确解释
unknown 与 violated 区分正确
引用 step 真实存在
progress 没有被当成商品证据
没有读取 reward/gold/termination_reason
每个 requirement 恰好一个 verdict
```

只有 Mock 和真实 smoke 均通过后，才允许全量：

```bash
python3 eval/trajectory_judge_v3.py \
  --input evaluations/h0 \
  --out evaluations/h0/judgments \
  --concurrency 4
```

正式输出：

```text
evaluations/h0/judgments/
├── <task_id>.json
├── manifest.json
└── summary.json
```

建议支持 `--only`、`--max-tasks`、`--concurrency`、`--timeout`、`--resume`、`--retry-failed`。已有合法结果跳过，损坏结果重做，失败结果只有显式重试才重新请求。

## 验收标准

```text
[ ] 先通过 Mock/单元测试
[ ] 先完成 5–8 条真实 LLM smoke test
[ ] smoke 输出不污染正式 judgments
[ ] Judge 只读取冻结 Rubric、model_trace、progress 白名单
[ ] 不读取完整 TaskFacts 或 deterministic outcome
[ ] 需求变化只影响本次输出，不修改 Rubric
[ ] 每条 requirement 恰好一个 verdict
[ ] satisfied/violated 引用真实 step
[ ] 200 条全量运行前人工检查 smoke 结果
```
