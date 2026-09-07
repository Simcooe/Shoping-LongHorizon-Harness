# Shopping Evaluation Report v2

## Metadata

- benchmark: shopping-final-v1
- 任务: 200/200
- deterministic 协议: terminal-protocol-v2
- judge_mode: ['mock']（mock 判定仅用于链路验证，不是真实模型成绩。）
- schema: shopping-report-v2 / shopping-rubric-schema-v2

## Panel 1: Environment Outcome

| 指标 | 数值 |
|---|---|
| task_success | 60 (0.3) |
| 价格数据缺口（reward_unverifiable） | 9 |
| 动态环境结果 | unavailable（历史 run 无环境动态裁判） |

outcome 分布：

- success_gold: 59
- repeat_loop: 50
- success_partial_alternative: 39
- max_steps: 17
- reward_unverifiable: 9
- success_valid_alternative: 1
- wrong_purchase: 1

## Panel 2: Rubric Satisfaction（决策时刻有效约束）

| 状态 | 数量 |
|---|---|
| satisfied | 481 |
| violated | 0 |
| unknown | 212 |
| not_applicable | 0 |

- final_satisfaction_rate: 0.6941（分母 693，不含 N/A 与生命周期排除项）
- 初始公开约束对照满足率: 0.6942（分母 677）
- hard: 0.6995（579） / soft: 0.6667（114）
- 生命周期: {'active': 693, 'superseded': 15}
- 澄清约束（时间线）: 16；最终有效: {'satisfied': 11, 'unknown': 5}
- 条件许可: 0（{}），单独展示

## Panel 3: Trajectory Quality

| 维度 | 均值 | 0 | 1 | 2 | 样本 |
|---|---|---|---|---|---|
| clarification_strategy | 1.985 | 0 | 3 | 197 | 200 |
| information_retention | 1.51 | 0 | 98 | 102 | 200 |
| search_strategy | 1.91 | 0 | 18 | 182 | 200 |
| candidate_utilization | 1.87 | 1 | 24 | 175 | 200 |
| evidence_verification | 1.91 | 0 | 18 | 182 | 200 |
| decision_quality | 1.68 | 2 | 60 | 138 | 200 |
| termination_efficiency | 1.48 | 35 | 34 | 131 | 200 |

按是否澄清分组（样本数/均值）：
- no_clarification: n=102, decision_quality=1.7941
- with_clarification: n=98, decision_quality=1.5612

## Panel 4: Behavior

- 决策类型: {'hard_stop': 44, 'finish': 15, 'purchase_attempt': 139, 'agent_stop': 2}
- 异常（任务数）: {'no_progress': 48, 'post_terminal_action': 34, 'inferred_invalid_navigation': 125, 'inferred_invalid_option': 41, 'inferred_invalid_buy': 43, 'inferred_invalid_click': 13, 'repeated_action': 2}
- 异常（动作数）: {'no_progress': 48, 'post_terminal_action': 179, 'inferred_invalid_navigation': 217, 'inferred_invalid_option': 53, 'inferred_invalid_buy': 44, 'inferred_invalid_click': 16, 'repeated_action': 2}
- 事件同步问题: {}
- 终局后事件（排除）: 5

---

*v2 报告只合并统计；mock judge_mode 的结果仅用于链路验证，不是真实模型成绩。*
