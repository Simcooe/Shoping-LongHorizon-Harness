# mea-v3 Harness Evaluation Report

- Tasks: 200
- Deterministic evaluator: `deterministic-evaluator-v3`
- Judge: `trajectory-judge-v3` (`deepseek-v4-flash`)
- Rubric: `shopping-rubric-v2`
- Terminal protocol: `terminal-protocol-v2`

The four panels are independent. No weighted aggregate score is produced.

## Panel 1 — Environment outcome

| Metric | Count | Denominator | Rate |
|---|---:|---:|---:|
| Environment terminal | 182 | 200 | 91.0% |
| Environment task success | 54 | 200 | 27.0% |
| Gold success | 54 | 200 | 27.0% |
| Purchase receipt observed | 92 | 200 | 46.0% |

Outcome classes:

| Class | Tasks |
|---|---:|
| `early_abstain` | 1 |
| `graceful_stop` | 2 |
| `max_steps` | 28 |
| `no_terminal_observed` | 18 |
| `partial_alternative_purchase` | 37 |
| `repeat_loop` | 59 |
| `success_gold` | 54 |
| `wrong_purchase` | 1 |

## Panel 2 — User requirement satisfaction

| Requirement group | Satisfied | Violated | Unknown | N/A | Evaluated | Rate |
|---|---:|---:|---:|---:|---:|---:|
| Explicit | 461 | 0 | 198 | 1 | 659 | 70.0% |
| TaskFact effective | 185 | 1 | 95 | 0 | 281 | 65.8% |

Latent TaskFacts: 503 (excluded from user satisfaction rates).

Decision summary:

| Metric | Count | Denominator | Rate |
|---|---:|---:|---:|
| Decision observed | 186 | 200 | 93.0% |
| Requirements resolved | 76 | 200 | 38.0% |

## Panel 3 — Process quality (0–2)

| Dimension | Mean | Score 0 | Score 1 | Score 2 |
|---|---:|---:|---:|---:|
| `clarification_strategy` | 1.565 | 7 | 73 | 120 |
| `information_retention` | 1.955 | 0 | 9 | 191 |
| `search_strategy` | 1.610 | 0 | 78 | 122 |
| `candidate_utilization` | 1.555 | 1 | 87 | 112 |
| `evidence_verification` | 1.295 | 3 | 135 | 62 |
| `decision_quality` | 1.395 | 18 | 85 | 97 |
| `termination_efficiency` | 1.100 | 16 | 148 | 36 |

## Panel 4 — Deterministic behavior

| Anomaly | Tasks | Occurrences | Task rate |
|---|---:|---:|---:|
| `inferred_adjacent_repeat` | 10 | 17 | 5.0% |
| `inferred_invalid_buy` | 1 | 1 | 0.5% |
| `inferred_invalid_click` | 23 | 26 | 11.5% |
| `inferred_invalid_click_all` | 114 | 200 | 57.0% |
| `inferred_invalid_navigation` | 90 | 137 | 45.0% |
| `inferred_invalid_option` | 29 | 36 | 14.5% |
| `invalid_click_unverifiable` | 0 | 0 | 0.0% |
| `no_progress` | 53 | 53 | 26.5% |
| `post_terminal_control_call` | 14 | 14 | 7.0% |
| `post_terminal_shopping_action` | 1 | 1 | 0.5% |
| `post_terminal_terminal_response` | 0 | 0 | 0.0% |
| `repeated_action` | 6 | 6 | 3.0% |

Tool-call counts:

| Scope | Mean | Median | P95 |
|---|---:|---:|---:|
| Full trajectory | 26.755 | 27.0 | 45 |
| Through first terminal | 26.680 | 27.0 | 45 |

## Interpretation notes

- 四个面板相互独立，不生成加权总分。
- latent TaskFacts 只报告页面证据，不计为用户需求 satisfied/violated。
- 缺失证据计 unknown，不计 violated。
- 确定性 inferred_* 行为是基于公开状态重放的推断，不等于环境真值。
