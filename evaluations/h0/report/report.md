# h0 Harness Evaluation Report

- Tasks: 200
- Deterministic evaluator: `deterministic-evaluator-v3`
- Judge: `trajectory-judge-v3` (`deepseek-v4-flash`)
- Rubric: `shopping-rubric-v2`
- Terminal protocol: `terminal-protocol-v2`

The four panels are independent. No weighted aggregate score is produced.

## Panel 1 — Environment outcome

| Metric | Count | Denominator | Rate |
|---|---:|---:|---:|
| Environment terminal | 176 | 200 | 88.0% |
| Environment task success | 60 | 200 | 30.0% |
| Gold success | 59 | 200 | 29.5% |
| Purchase receipt observed | 109 | 200 | 54.5% |

Outcome classes:

| Class | Tasks |
|---|---:|
| `max_steps` | 17 |
| `no_terminal_observed` | 24 |
| `partial_alternative_purchase` | 39 |
| `repeat_loop` | 50 |
| `reward_unverifiable` | 9 |
| `success_gold` | 59 |
| `success_valid_alternative` | 1 |
| `wrong_purchase` | 1 |

## Panel 2 — User requirement satisfaction

| Requirement group | Satisfied | Violated | Unknown | N/A | Evaluated | Rate |
|---|---:|---:|---:|---:|---:|---:|
| Explicit | 519 | 2 | 138 | 1 | 659 | 78.8% |
| TaskFact effective | 150 | 0 | 39 | 0 | 189 | 79.4% |

Latent TaskFacts: 594 (excluded from user satisfaction rates).

Decision summary:

| Metric | Count | Denominator | Rate |
|---|---:|---:|---:|
| Decision observed | 198 | 200 | 99.0% |
| Requirements resolved | 114 | 200 | 57.0% |

## Panel 3 — Process quality (0–2)

| Dimension | Mean | Score 0 | Score 1 | Score 2 |
|---|---:|---:|---:|---:|
| `clarification_strategy` | 1.405 | 8 | 103 | 89 |
| `information_retention` | 1.750 | 1 | 48 | 151 |
| `search_strategy` | 1.360 | 2 | 124 | 74 |
| `candidate_utilization` | 1.290 | 2 | 138 | 60 |
| `evidence_verification` | 1.105 | 6 | 167 | 27 |
| `decision_quality` | 1.430 | 35 | 44 | 121 |
| `termination_efficiency` | 1.025 | 22 | 151 | 27 |

## Panel 4 — Deterministic behavior

| Anomaly | Tasks | Occurrences | Task rate |
|---|---:|---:|---:|
| `inferred_adjacent_repeat` | 9 | 11 | 4.5% |
| `inferred_invalid_buy` | 43 | 44 | 21.5% |
| `inferred_invalid_click` | 13 | 16 | 6.5% |
| `inferred_invalid_click_all` | 149 | 331 | 74.5% |
| `inferred_invalid_navigation` | 125 | 218 | 62.5% |
| `inferred_invalid_option` | 41 | 53 | 20.5% |
| `invalid_click_unverifiable` | 0 | 0 | 0.0% |
| `no_progress` | 48 | 48 | 24.0% |
| `post_terminal_control_call` | 0 | 0 | 0.0% |
| `post_terminal_shopping_action` | 34 | 179 | 17.0% |
| `post_terminal_terminal_response` | 33 | 75 | 16.5% |
| `repeated_action` | 2 | 2 | 1.0% |

Tool-call counts:

| Scope | Mean | Median | P95 |
|---|---:|---:|---:|
| Full trajectory | 18.465 | 16.0 | 38 |
| Through first terminal | 17.570 | 15.0 | 36 |

## Interpretation notes

- 四个面板相互独立，不生成加权总分。
- latent TaskFacts 只报告页面证据，不计为用户需求 satisfied/violated。
- 缺失证据计 unknown，不计 violated。
- 确定性 inferred_* 行为是基于公开状态重放的推断，不等于环境真值。
