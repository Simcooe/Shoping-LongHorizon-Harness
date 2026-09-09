# Deterministic evaluation v3

This directory was produced offline by `deterministic-evaluator-v3` using
`terminal-protocol-v2`. The evaluator read only `manifest.json` and the exported
model/raw traces. It did not run DSH, ShopSimulator, or an LLM, and did not read
rubrics, sessions, source goals, judgments, summaries, or private TaskFacts.

## Semantics

- The first raw `done is true` response is the terminal and cannot be replaced.
- `gold_success` is only `gold_purchase`; `environment_task_success` additionally
  accepts `valid_alternative_purchase`.
- `purchase_occurred` requires a valid receipt (`asin`, `name`, `price`, and
  `options`); it does not mean the purchase was correct.
- Shopping tools are `search`, `click`, `ask_shopper`, and `finish`. Environment
  action calls are only `search`, `click`, and `finish`; `ask_shopper` does not
  advance ShopSimulator. `mea_round_report` is a control tool.
- Full-trajectory counts and counts through the first terminal (inclusive) are
  reported separately. P95 uses nearest-rank (`ceil(0.95*n)-1` zero-based).
- Action-before state starts at `reset.observation_state` and advances only on a
  new raw `observation_state`. `ask_shopper` and control tools retain the page.
  Missing environment evidence invalidates the page until a new state appears.
- Missing `actions` means click legality is unknown; an explicit empty list is a
  known empty set.
- Primary outcome rates use every unique manifest task as the denominator.

`summary.json` records evaluator thresholds and SHA-256 hashes for every input.
`task_results.jsonl` contains per-task evidence references. The environment
reward class is an environment metric, not a semantic judge of the user's public
request.
