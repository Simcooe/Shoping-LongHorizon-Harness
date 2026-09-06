# Shopping Evaluation Report

## Benchmark / Run Metadata

| 项 | 值 |
|---|---|
| benchmark_id | shopping-final-v1 |
| task_count | 200 |
| evaluated_task_count | 200 |
| profile | h0 |
| run_dir | runs/h0-0905-1446 |
| rubric_version | v1 |
| rubric_strategy | initial_query_only_v1 |
| judge_model | qwen3.8-max |
| judge_trace_source | model_trace_only |
| environment_version | shopsimulator-environment-v2.1 |
| generated_at | 2026-09-06T00:07:46.764843+00:00 |

## Panel 1: Environment Outcome

| 指标 | 数值 |
|---|---|
| total | 200 |
| environment_terminal_count / rate | 176 / 88.0% |
| task_success_count / rate | 65 / 32.5% |
| outcome_null_count | 24 |

outcome class 分布（task_success 口径以 deterministic evaluator 为准，success_partial_alternative 不计入 task_success）：

| outcome class | 数量 |
|---|---|
| success_gold | 64 |
| success_partial_alternative | 39 |
| repeat_loop | 38 |
| (null) | 24 |
| reward_unverifiable | 10 |
| early_abstain | 9 |
| max_steps | 8 |
| graceful_stop | 6 |
| success_valid_alternative | 1 |
| wrong_purchase | 1 |

## Panel 2: Rubric Satisfaction

| 状态 | 数量 | 比例 |
|---|---|---|
| satisfied | 611 | 88.3% |
| violated | 8 | 1.2% |
| unknown | 73 | 10.5% |
| not_applicable | 0 | 0.0% |

| 指标 | 数值 |
|---|---|
| total_constraints | 692 |
| overall_satisfaction_rate | 88.3% |
| hard_satisfaction_rate | 89.3% |
| soft_satisfaction_rate | 83.7% |
| hard_violation_rate | 1.2% |
| unknown_rate | 10.5% |
| hardness_distribution | {"hard": 563, "soft": 129} |
| source_distribution | {"initial_query": 692} |

## Panel 3: Trajectory Quality

| 维度 | 平均分 | 0 分 | 1 分 | 2 分 |
|---|---|---|---|---|
| clarification_strategy | 1.945 | 0 | 11 | 189 |
| information_retention | 1.95 | 0 | 10 | 190 |
| search_strategy | 1.32 | 0 | 136 | 64 |
| candidate_utilization | 1.32 | 1 | 134 | 65 |
| evidence_verification | 1.245 | 1 | 149 | 50 |
| decision_quality | 1.46 | 4 | 100 | 96 |
| termination_efficiency | 1.08 | 9 | 166 | 25 |

所有维度平均分：1.4743

ask_shopper：使用率 49.0%（98/200 任务），总调用 134 次，平均 0.67 次/任务

| 维度 | 有 ask_shopper 均分 | 无 ask_shopper 均分 |
|---|---|---|
| clarification_strategy | 1.888 | 2.0 |
| information_retention | 1.908 | 1.99 |
| search_strategy | 1.337 | 1.304 |
| candidate_utilization | 1.408 | 1.235 |
| evidence_verification | 1.245 | 1.245 |
| decision_quality | 1.439 | 1.48 |
| termination_efficiency | 1.051 | 1.108 |

## Panel 4: Deterministic / Harness Behavior

| 指标 | 数值 |
|---|---|
| 平均步数 | 18.46 |
| 中位步数 | 16.0 |
| 最小 / 最大步数 | 4 / 47 |
| non_terminal_count | 24 |
| agent_turn_completed | 200 / 100.0% |
| post_terminal_action_count | 15 |

anomaly 分布：

| anomaly | 数量 |
|---|---|
| inferred_invalid_navigation | 126 |
| no_progress | 48 |
| inferred_invalid_buy | 43 |
| inferred_invalid_option | 41 |
| superseded_hard_stop | 21 |
| post_terminal_action | 15 |
| inferred_invalid_click | 13 |
| repeated_action | 2 |

failure classes：

- **invalid_buy_then_false_completion** (22): 98, 284, 311, 436, 467, 473, 514, 584, 624, 732, 808, 819, 820, 890, 1015, 1134, 1140, 1188, 1196, 1219…
- **non_terminal_agent_stop** (2): 47, 204

## Per-task Examples

### gold success — task 25

> Query: 麻烦帮我找一款液体修容盘，价格在90元左右。

- Panel 1: class=success_gold, reward_type=gold_purchase, task_success=True
- Panel 2: satisfied=3, violated=0, unknown=0
  - c0001 [hard] 商品品类为修容盘 → satisfied (steps [2, 12, 16])
  - c0002 [hard] 质地/形态为液体 → satisfied (steps [2, 12, 16])
  - c0003 [soft] 价格预期约90元 → satisfied (steps [14, 15, 16])
- Panel 3: clarification_strategy=2, information_retention=2, search_strategy=1, candidate_utilization=1, evidence_verification=1, decision_quality=2, termination_efficiency=1
- Panel 4: steps=17, anomalies=['inferred_invalid_navigation']

### invalid_buy_then_false_completion — task 98

> Query: 请找一款价格在300元左右的平板支架。

- Panel 1: class=None, reward_type=None, task_success=False
- Panel 2: satisfied=2, violated=0, unknown=0
  - c0001 [hard] 商品品类为平板支架 → satisfied (steps [6, 7])
  - c0002 [soft] 价格预期约300元 → satisfied (steps [7])
- Panel 3: clarification_strategy=2, information_retention=1, search_strategy=2, candidate_utilization=2, evidence_verification=2, decision_quality=1, termination_efficiency=1
- Panel 4: steps=12, anomalies=['inferred_invalid_buy', 'inferred_invalid_navigation']

### repeat_loop — task 6

> Query: 想要红色手柄的水暖排气阀，价格在30元左右。

- Panel 1: class=repeat_loop, reward_type=repeat_loop, task_success=False
- Panel 2: satisfied=3, violated=0, unknown=0
  - c0001 [hard] 商品品类为水暖排气阀 → satisfied (steps [2, 14, 28])
  - c0002 [hard] 手柄颜色为红色 → satisfied (steps [14, 15, 28, 29])
  - c0003 [soft] 价格预期约30元 → satisfied (steps [14, 15, 28, 29])
- Panel 3: clarification_strategy=2, information_retention=2, search_strategy=1, candidate_utilization=1, evidence_verification=1, decision_quality=1, termination_efficiency=1
- Panel 4: steps=29, anomalies=['no_progress']

### non_terminal_agent_stop — task 47

> Query: 我想要买秋天9月后种的银莲花球根。

- Panel 1: class=None, reward_type=None, task_success=False
- Panel 2: satisfied=2, violated=0, unknown=0
  - c0001 [hard] 商品品类为银莲花球根 → satisfied (steps [1, 2, 6])
  - c0002 [hard] 适合在秋天9月以后种植 → satisfied (steps [1, 2, 6])
- Panel 3: clarification_strategy=1, information_retention=1, search_strategy=1, candidate_utilization=1, evidence_verification=1, decision_quality=1, termination_efficiency=1
- Panel 4: steps=12, anomalies=['inferred_invalid_navigation']

### with ask_shopper — task 6

> Query: 想要红色手柄的水暖排气阀，价格在30元左右。

- Panel 1: class=repeat_loop, reward_type=repeat_loop, task_success=False
- Panel 2: satisfied=3, violated=0, unknown=0
  - c0001 [hard] 商品品类为水暖排气阀 → satisfied (steps [2, 14, 28])
  - c0002 [hard] 手柄颜色为红色 → satisfied (steps [14, 15, 28, 29])
  - c0003 [soft] 价格预期约30元 → satisfied (steps [14, 15, 28, 29])
- Panel 3: clarification_strategy=2, information_retention=2, search_strategy=1, candidate_utilization=1, evidence_verification=1, decision_quality=1, termination_efficiency=1
- Panel 4: steps=29, anomalies=['no_progress']

---

*本报告只合并已有评测产物：Panel 1/4 来自 deterministic evaluator，Panel 2/3 来自 model_trace-only LLM Judge + 冻结 Query-grounded Rubric。不包含任何 gold ASIN / hidden TaskFacts，各面板不合成单一总分。*
