# Evaluate Report: h0-0905-1446

- 总任务数: 200
- 环境终局 (environment_done): 176 (88.0%)
- 任务成功 (task_success): 65 (32.5%)
- Agent turn completed: 200 (100.0%)
- post_terminal_action: 15
- non_terminal: 24

## Environment Outcomes

| outcome | count |
|---|---|
| success_gold | 64 |
| success_partial_alternative | 39 |
| repeat_loop | 38 |
| reward_unverifiable | 10 |
| early_abstain | 9 |
| max_steps | 8 |
| graceful_stop | 6 |
| success_valid_alternative | 1 |
| wrong_purchase | 1 |

## Non-terminal / Failure Causes

| failure | count |
|---|---|
| invalid_buy_then_false_completion | 22 |
| non_terminal_agent_stop | 2 |

## Anomalies

| anomaly | count |
|---|---|
| inferred_invalid_navigation | 126 |
| no_progress | 48 |
| inferred_invalid_buy | 43 |
| inferred_invalid_option | 41 |
| superseded_hard_stop | 21 |
| post_terminal_action | 15 |
| inferred_invalid_click | 13 |
| repeated_action | 2 |
