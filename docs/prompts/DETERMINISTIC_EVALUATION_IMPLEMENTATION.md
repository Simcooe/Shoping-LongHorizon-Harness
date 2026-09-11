# 确定性评测指标实现任务

## 1. 目标与边界

实现独立的离线确定性评测器，读取 DSH 已落盘的 model_trace/raw_trace，生成环境结果、工具统计、行为异常和输入完整性报告。

DSH 仍是任务执行入口。本任务不启动 DSH/ShopSimulator、不调用 LLM、不改 Harness、不生成或修改 Rubric、不实现 Judge、不读取 MEA 日志或私有 TaskFacts。不要扩大为新的 Agent 执行系统。

先阅读实际数据与代码：

- `evaluations/h0/manifest.json`
- `evaluations/h0/traces/*.model_trace.json`、`*.raw_trace.json`（抽样及全量结构检查）
- `eval/evaluate.py`
- `eval/trace_utils.py`
- `scripts/export_trace.py`
- 已有 terminal/export/profile gating 测试

旧代码供理解协议和复用纯函数，不要求继承其所有分类。特别是无证据时的误分类必须修正。

## 2. 输入与输出

输入默认 `evaluations/h0/`，只读取其 manifest 和 traces。

- manifest 只用于任务集合、profile、运行元数据，不能读取其中 reward/status 当任务结果。
- task ID 从 manifest 的实际结构读取（当前 run manifest 使用 `goals[].task_id`），不要假定必有 `task_ids`。
- raw trace 内的 `reset` 可用于初始状态重放；不需要额外 reset 文件。
- 不读取 Rubric：当前指标不需要语义约束。
- 不读取 source_goals、旧 judgment、旧 summary、session 或其他 run。

新增建议：`eval/deterministic_v3.py`、`eval/tests/test_deterministic_v3.py`。必要时拆小型纯函数模块，不大规模重构旧链路。

离线调用示例（不是任务执行入口）：

```bash
python3 eval/deterministic_v3.py --input evaluations/h0 --out evaluations/h0/deterministic
```

输出：

```text
evaluations/h0/deterministic/
├── task_results.jsonl
├── summary.json
├── failure_breakdown.json
└── README.md
```

原始 trace、manifest、Rubric 不得被修改。记录输入文件 SHA-256、evaluator 版本、终局协议和指标阈值，保证可复现。

## 3. 指标范围

第一版四组指标，不计算综合总分。

### A. 输入完整性

- manifest 任务数、重复 ID、缺失/额外 trace。
- JSON 是否可解析；steps 是否为列表；声明 step_count 是否匹配实际长度。
- model/raw 长度及逐项 step、tool_name、tool_args 是否一致。
- raw 缺失位置、协议缺失或冲突、无结果的可观测缺口。

参数按结构化 JSON 比较，不比较序列化字符串顺序。

以数组 index 为本次导出位置，保留原 step 作为辅助字段。DSH step 可能不是全局唯一工具调用 ID，不能仅因重复 step 就断言轨迹错误，更不能按 step 字典覆盖记录。无稳定 call ID 时使用 index + step + tool_name + arguments 验证对齐，不猜测配对。

缺 raw 需区分：

- 环境工具缺 raw：环境证据不完整。
- `mea_round_report` 等控制工具无环境 raw：可能是预期行为，不自动将整条轨迹判无效。
- ask_shopper 不一定返回购物页面状态；没有 observation_state 不等于 raw 缺失。

### B. 环境结果

从 raw steps 锁定第一条 `raw.done is True`，使用 terminal-protocol-v2。后续终局不能覆盖它。

保留：environment_done、terminal_index、原 step、reward_type、termination_reason、reward、reward_valid、环境成功标记、购买回执摘要。

reward_type 优先 `raw.reward_detail.reward_type`，回退 termination_reason；未知类型显式保留，不擅自归类。

映射：

| 原类型 | outcome.class |
|---|---|
| gold_purchase | success_gold |
| valid_alternative_purchase | success_valid_alternative |
| partial_alternative_purchase | partial_alternative_purchase |
| wrong_purchase | wrong_purchase |
| graceful_stop | graceful_stop |
| early_abstain | early_abstain |
| repeat_loop | repeat_loop |
| max_steps | max_steps |
| reward_unverifiable | reward_unverifiable |

成功指标明确命名：

- gold_success：仅 gold_purchase。
- environment_task_success：gold_purchase 或 valid_alternative_purchase。
- purchase_occurred：是否有有效购买回执，不等同购买正确，也不等同点击 Buy Now。

先检查实际 purchase 结构和环境写入规则，再实现回执判定。空字典、缺字段不能靠 bool(raw.purchase) 草率推断。缺少足够证据时为 null/unknown；不要用成功率反推购买率。

环境 reward 分类是“环境口径”，不是公开用户需求满足的最终真理。报告注明这一点。只输出 purchase 的必要白名单字段；不复制 hidden goal、gold reference 等私有内容。实际所购商品 ID 与 gold reference 是不同概念。

### C. 工具与长度

- total_tool_calls：所有已配对工具事件。
- shopping_tool_calls：search/click/ask_shopper/finish。
- environment_action_calls：按实际 shop-tools 协议定义并说明，不能默认 ask_shopper 等同环境 step。
- control_tool_counts：mea_round_report 等。
- other_tool_counts：其他工具名，不丢弃。
- 各工具次数、购物工具调用数 mean/median/P95。

分别保留全轨迹和终局前（含终局）计数。没有终局时范围为整条可用轨迹。

P95 固定使用 nearest-rank（ceil(0.95*n)-1）并测试；空集合返回 null。

不把工具次数伪装为 token 成本或延迟；trace 未记录 usage/timestamps 时，不估造成本。

### D. 行为诊断

1. `repeated_action`：读取真实 `raw.progress.consecutive_repeats >= 2` 的位置。
2. `no_progress`：读取真实 `raw.progress.no_progress_steps >= 4` 的位置。
3. `inferred_adjacent_repeat`：可选，工具名和规范化参数连续相同，独立于环境 progress 指标。重复不必然无效。
4. `inferred_invalid_click` 及子类 invalid_buy/invalid_option/invalid_navigation。
5. `post_terminal_shopping_action`：第一终局后 search/click/ask_shopper/finish 调用。
6. `post_terminal_control_call`：终局后控制工具调用，单独统计，不混同购物动作。

progress 缺失表示 unavailable，不表示“无重复/无进展”。只记录达到阈值的真实 step，不能根据一个计数为4的 step 凭空补出前面4条异常记录。

第一终局之前不可能还有 done=true 的硬终止，因此 v2 不实现自相矛盾的 superseded_hard_stop 主指标。若需要描述终局后的再次 done，命名 `post_terminal_terminal_response`，仅诊断，不覆盖结果。

## 4. 动作前状态重放

raw observation_state 是动作后状态。前状态来源为 raw_trace.reset 中公开初始 observation_state，或上一条可靠环境观察。

要求：

- 从实际 reset 结构提取，不把整个 reset payload 误当 observation_state。
- 有新的有效 observation_state 才更新页面。
- ask_shopper/控制工具不应无故清空页面。
- 对可能改变环境却缺失结果/状态的工具，将后续前状态可靠性降为 unknown，直到获得新可靠观察；不要跨缺口继续使用陈旧状态当证据。
- 明确空 actions=[] 是已知空集合；缺 actions 是未知，不得等同。
- 缺前状态时返回 legality=unknown，不用动作后状态反推本次动作合法性。

click value 与动作前 actions 按环境实际协议归一化，先检查已有 normalize/guard 实现，避免额外宽松归一化掩盖错误。

分类：

- buy now 不在动作前 actions：inferred_invalid_buy。
- information_subpage 上不存在的点击：inferred_invalid_navigation。
- product_detail 非法导航：inferred_invalid_navigation。
- product_detail 其他非法点击：inferred_invalid_option（注明启发式分类）。
- 其他：inferred_invalid_click。

invalid_click_unverifiable 属于覆盖率诊断，不是非法动作异常。

## 5. 不完整与非终局的处理

- 缺文件/坏 JSON：仍为该 task 写结果；受影响指标为 null，并记录错误。
- 无终局但 raw 完整：只说明 no_terminal_observed，不能断言基础设施错误、主动放弃或虚假完成。
- 无 session/assistant final 证据时，不生成 invalid_buy_then_false_completion、non_terminal_agent_stop 等带意图的结论。
- 轨迹不完整且未观察到 done：不能断言环境没有结束，environment_done 设 null，记录 no_terminal_observed 与 evidence_incomplete。
- 已观察第一终局但此前有环境证据缺口：标记终局选择可信度不足，不能无条件声称这是实际第一次终局。
- 对齐失败时，单侧可计算指标可保留，但跨侧结论不可继续静默生成。

## 6. 输出与分母

每个 task 至少包含：

```json
{
  "task_id": "263",
  "evaluator_version": "deterministic-evaluator-v3",
  "terminal_protocol": "terminal-protocol-v2",
  "trace_integrity": {},
  "outcome": {},
  "behavior": {
    "tool_counts": {},
    "control_tool_counts": {},
    "anomalies": []
  },
  "coverage": {},
  "errors": []
}
```

每个诊断记录 source、index、原 step、tool_name、basis 和 evidence refs。例如 `raw:/steps/8/raw/observation_state`。重放合法性需同时引用动作和支撑前状态的记录。index 是0基；原 step 保持原值。

summary 至少汇总：

- manifest 预期任务数、实际可读取数、完整性通过数。
- gold_success、environment_task_success、environment_terminal、purchase_occurred 的 count/rate/unknown_count。
- outcome 类别分布（包含未知/无终局）。
- shopping_tool_calls mean/median/P95；各工具调用量。
- repeated_action、no_progress、inferred_invalid_click（含子类）、post_terminal_shopping_action 的任务数和次数。
- progress_available、click_legality_checkable 等覆盖率。

主结果分母使用 manifest 全任务集合，缺失/失败不从分母中移除。对只适用于某类事件的指标，额外明确 eligible_count、evaluated_count、unknown_count。无可评估事件时比例 null，不是0或100%。

failure_breakdown 中区分：

- task_count：出现该异常的任务数。
- occurrence_count：触发位置数，不是推测根因事件数。
- task_rate：任务数/全任务数。

非法 click 总数按所有子类 index 的并集统计，不能重复加总。文件完整性错误、环境终局类别、行为异常分别展示，不混成互斥“失败原因”。

## 7. 测试与运行

测试至少覆盖：

- 正常输入、缺文件、坏 JSON、长度/参数/工具名错配。
- 重复 DSH step 但位置配对正确；控制工具预期 raw_missing。
- 首次 done 锁定；repeat_loop 后 gold_purchase 不改成功。
- 无终局与证据缺失区分；未知终局类型。
- 有/无/空/畸形购买回执；购买发生和购买正确分开。
- reset 前状态、合法 click、非法 buy/option/navigation。
- 缺前状态、缺 actions、空 actions、环境事件缺口导致状态失效。
- ask_shopper/control 工具保留可靠页面。
- progress 阈值、缺 progress、终局后 progress 不污染主过程。
- 终局后购物调用与控制调用分开。
- 固定分母、未知值、空集合、P95、任务数与出现次数。

测试使用 fixture，不调用模型或环境。优先沿用项目现有 unittest/pytest 方式。

测试通过后运行 h0 的200条，确认：

- task_results.jsonl 恰好覆盖 manifest 的200个唯一 task_id。
- 输出实际指标，不硬编码本文件或历史回复中的示例数字。
- 所有输入 SHA-256 运行前后不变。
- 再次运行相同输入，去掉生成时间等元数据后结果一致。
- 缺失/额外/损坏输入写完整诊断后以非零状态退出；不能静默成功。

不要假定旧 reports 仍在本地（用户已移除旧评测产物）。如能利用旧纯函数做兼容对照，可作为附加测试；无历史结果不阻塞本次交付，更不能重新创建假历史数值。

## 8. 验收汇报

执行会话报告：

1. 新增/修改文件。
2. 输入输出位置与只读范围。
3. 各指标定义、分母、不可测字段。
4. terminal 协议和前状态重放规则。
5. 单元测试结果。
6. h0 200条真实汇总与覆盖率。
7. 发现的数据问题及其 task/index 引用。
8. 输入未修改的 hash 校验结果。

本阶段结束于确定性报告，不继续实现 Judge 或改 Harness。
