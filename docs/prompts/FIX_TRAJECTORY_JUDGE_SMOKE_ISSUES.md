# 修复 Trajectory Judge Smoke Test 暴露的问题

## 1. 背景

当前 Judge 已实现：

```text
eval/trajectory_judge_v3.py
eval/tests/test_trajectory_judge_v3.py
```

真实 smoke 输出位于：

```text
/tmp/judge-smoke/
```

当前代码测试已经通过，但真实 smoke 暴露了几个语义和输出一致性问题。本次任务只修复这些问题，不执行 200 条全量 Judge。

---

## 2. 必须修复的问题

### P0-1：修复 smoke manifest

输出目录中实际可能存在多个 task 结果，但 manifest 可能只记录最后一次 `--only` 运行的单个 task。Manifest 必须只描述本次命令请求的 task 集合和本次执行结果。

建议结构：

```json
{
  "schema": "shopping-judge-manifest-v3",
  "judge_version": "trajectory-judge-v3",
  "rubric_version": "shopping-rubric-v2",
  "judge_mode": "llm",
  "input_dir": "evaluations/h0",
  "requested_task_ids": [25, 47, 108, 133, 263, 596, 602],
  "task_count": 7,
  "succeeded": [25, 47, 108, 133, 263, 596, 602],
  "failed": [],
  "skipped": [],
  "generated_at": "..."
}
```

如果命令为：

```bash
--only 263
```

则本次 manifest 只描述 task 263：

```json
{
  "requested_task_ids": [263],
  "task_count": 1,
  "succeeded": [],
  "skipped": [263],
  "failed": []
}
```

不要因为输出目录中已有其他 task 文件，把历史结果混入本次 manifest。`summary.json` 也只能统计本次请求的 task 集合。

---

### P0-2：初始 Query 的证据必须来自 Rubric

初始 Query requirement 不能使用如下伪证据：

```json
{
  "source": "model_trace",
  "step": 1,
  "field": "tool_args"
}
```

初始需求来自冻结 Rubric：

```json
{
  "source": "rubric",
  "field": "query_quote",
  "rubric_id": "r0001"
}
```

或者：

```json
{
  "source": "rubric",
  "field": "query",
  "rubric_id": "r0001"
}
```

规则：

```text
initial_query：
  来源必须是 rubric

query_and_taskfacts：
  初始有效性来源仍然是 rubric.query/query_quote
  TaskFacts basis 只作为辅助来源

taskfacts：
  初始状态不能用 model_trace step 1 tool_args 证明
```

`model_trace` 只能证明 Agent 后续看到了什么、做了什么，不能证明初始用户需求是什么。

---

### P0-3：shop_asker 激活证据必须指向真实用户回复

如果 TaskFacts requirement 被标记为：

```text
effective_status = active
```

则 basis 必须指向真正包含用户回复的 step：

```json
{
  "source": "model_trace",
  "step": 4,
  "field": "observation",
  "event": "shopper_reply",
  "rubric_id": "t0001"
}
```

如果 question 和 reply 出现在同一个 Observation：

```json
{
  "source": "model_trace",
  "step": 2,
  "field": "observation",
  "event": "ask_shopper_reply",
  "rubric_id": "t0001"
}
```

如果没有找到明确用户回复：

```text
effective_status 必须保持 latent/unknown
```

不能使用以下内容证明用户确认：

```text
model_trace step 1 observation
model_trace step 1 tool_args
商品详情 Observation
Agent 自己的总结
```

建议增加内部函数：

```python
extract_shopper_replies(model_trace)
```

返回：

```python
[
    {
        "step": 4,
        "question": "...",
        "reply": "...",
        "evidence_ref": {
            "source": "model_trace",
            "step": 4,
            "field": "observation",
            "event": "shopper_reply"
        }
    }
]
```

只有识别出的真实用户回复才能激活、修改、放宽、拒绝或撤销 TaskFacts requirement。

---

### P0-4：禁止 `latent + satisfied` 直接作为用户需求满足

不要输出这种语义混乱的结果：

```json
{
  "requirement_id": "t0002",
  "effective_status": "latent",
  "verdict": "satisfied"
}
```

页面上存在 TaskFacts 事实，与用户在本条轨迹中有效要求该事实，是两个不同问题。

建议每条 verdict 增加两个字段：

```json
{
  "requirement_id": "t0002",
  "requirement_kind": "taskfact",
  "effective_status": "latent",
  "user_requirement_verdict": "not_applicable",
  "evidence_status": "supported",
  "evidence": [
    {
      "source": "model_trace",
      "step": 15,
      "field": "observation"
    }
  ],
  "reasoning": "页面证据支持该 TaskFacts 属性，但用户从未通过 Query 或 shopper 回复确认该要求，因此不能作为本次用户需求的 satisfied。"
}
```

字段语义：

```text
effective_status：
  active / latent / modified / rejected / revoked / unknown

user_requirement_verdict：
  satisfied / violated / unknown / not_applicable

evidence_status：
  supported / contradicted / unknown
```

规则：

```text
如果 effective_status == latent：
  user_requirement_verdict 不能是 satisfied
  user_requirement_verdict 不能是 violated
```

推荐使用：

```text
user_requirement_verdict = not_applicable
```

或在不确定时：

```text
user_requirement_verdict = unknown
```

但如果页面证据存在，仍可以：

```text
evidence_status = supported
```

示例：

### 未激活，但页面支持

```json
{
  "effective_status": "latent",
  "user_requirement_verdict": "not_applicable",
  "evidence_status": "supported"
}
```

### 用户确认且页面支持

```json
{
  "effective_status": "active",
  "user_requirement_verdict": "satisfied",
  "evidence_status": "supported"
}
```

### 用户确认但页面不满足

```json
{
  "effective_status": "active",
  "user_requirement_verdict": "violated",
  "evidence_status": "contradicted"
}
```

### 未确认且没有页面证据

```json
{
  "effective_status": "latent",
  "user_requirement_verdict": "not_applicable",
  "evidence_status": "unknown"
}
```

---

## 3. Summary 统计规则

Summary 必须把以下内容分开统计：

```text
explicit requirement verdicts
active taskfact requirement verdicts
latent taskfact evidence status
```

建议结构：

```json
{
  "verdict_distribution": {
    "explicit": {
      "satisfied": 10,
      "violated": 2,
      "unknown": 4,
      "not_applicable": 0
    },
    "taskfact_active": {
      "satisfied": 3,
      "violated": 1,
      "unknown": 2,
      "not_applicable": 0
    },
    "taskfact_latent": {
      "count": 8,
      "evidence_supported": 4,
      "evidence_contradicted": 0,
      "evidence_unknown": 4
    }
  }
}
```

至少保证：

```text
latent TaskFacts 不进入普通 satisfied 统计
```

---

## 4. Decision 字段语义

不要让一个 `resolved` 字段同时表示“观察到了终止动作”和“用户需求已解决”。建议拆成：

```json
{
  "decision": {
    "observed": true,
    "step": 36,
    "kind": "finish",
    "requirements_resolved": false
  }
}
```

字段：

```text
observed：是否观察到 purchase/finish/environment terminal 决策
kind：purchase / finish / environment_terminal / unresolved
requirements_resolved：Judge 是否认为用户需求已经满足或明确处理
```

如果不改输出结构，至少明确旧 `resolved` 的语义，并禁止它同时表示任务成功。

---

## 5. Terminal Boundary 脱敏投影

Judge 不能读取 deterministic evaluator 结果，也不能看到 reward，但可以接收不泄漏结果的终局边界：

```json
{
  "runtime_boundary": {
    "first_terminal_observed": true,
    "terminal_step": 20,
    "post_terminal_steps_present": true
  }
}
```

允许：

```text
first_terminal_observed
terminal_step
post_terminal_steps_present
```

禁止：

```text
reward_type
reward
purchase_success
gold
termination_reason
```

该投影只帮助 Judge 区分“环境已终止但需求未满足”和“轨迹尚未完成”，不替代 model_trace 证据。

---

## 6. Smoke 重跑

修复后使用新空目录，避免历史输出和 resume 干扰：

```bash
rm -rf /tmp/judge-smoke-v2

python3 eval/trajectory_judge_v3.py \
  --input evaluations/h0 \
  --out /tmp/judge-smoke-v2 \
  --only 25,47,108,133,263,596,602 \
  --judge-mode llm \
  --concurrency 2
```

检查：

```text
task_count = 7
succeeded = 7
failed = 0
skipped = 0
```

不要执行 200 条全量 Judge。

---

## 7. 测试要求

更新：

```text
eval/tests/test_trajectory_judge_v3.py
```

至少增加：

### 初始 Query evidence

断言 explicit requirement 的 basis：

```text
source == rubric
```

禁止：

```text
model_trace step 1 tool_args
```

### Shopper activation evidence

有 ask_shopper reply 时：

```text
effective_status == active
basis 指向真实 reply step
```

没有 reply 时：

```text
effective_status == latent
```

### Latent evidence

latent TaskFacts + 页面证据支持时：

```text
user_requirement_verdict != satisfied
 evidence_status == supported
```

### Active taskfact

active TaskFacts + 页面证据支持时：

```text
user_requirement_verdict == satisfied
```

### Manifest

使用 `--only` 多 task 时：

```text
manifest.task_count == requested task count
manifest.succeeded/failed/skipped 只描述本次请求
```

### Decision

`finish` 可以：

```text
observed == true
requirements_resolved == false
```

---

## 8. 本次不做

不要：

```text
修改冻结 Rubric
重新生成 200 个 Rubric
读取 source_goals.private.jsonl
读取完整 raw_trace 给 LLM
把 reward/gold 给 LLM
直接跑 200 条正式 Judge
修改 DSH/Harness
修改 deterministic evaluator
```

---

## 9. 完成标准

```text
[ ] smoke manifest 与本次任务集合一致
[ ] 初始 Query evidence 来自 Rubric
[ ] shopper 激活引用真实 reply step
[ ] latent + satisfied 不再出现
[ ] taskfact 的 evidence_status 与 user_requirement_verdict 分离
[ ] decision 字段语义清晰
[ ] terminal boundary 可脱敏提供
[ ] 新增测试通过
[ ] 重新执行 7 条 smoke
[ ] smoke 结果人工检查通过
[ ] 没有运行 200 条全量 Judge
```

## 最终原则

```text
冻结 Rubric：定义需要评估的候选要求
初始需求 evidence：来自 Rubric，不来自 Agent 工具调用
Shopper reply：只有真实用户回复才能激活 TaskFacts requirement
TaskFacts 页面 evidence：只能说明商品事实被观察到，不能自动说明用户需求 satisfied
Judge：同时输出用户需求 verdict 和事实证据状态
```

核心修复目标：

> **把“页面上存在 TaskFacts 事实”和“用户在本次轨迹中有效要求该事实”严格分开。**
