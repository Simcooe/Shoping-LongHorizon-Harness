# h0 Evaluation Input

- Harness/profile: `h0`
- Source DSH run: `runs/h0-0905-1446`
- Benchmark: `shopping-final-v1`
- Task count: 200

## Files

- `manifest.json`: DSH run metadata and task inventory.
- `traces/*.model_trace.json`: model-visible trajectory.
- `traces/*.raw_trace.json`: environment-native trajectory.

This directory is the frozen input for the rebuilt evaluation pipeline. Existing
Judge, rubric, and deterministic evaluation outputs are intentionally excluded
and will be regenerated from the two trace views.
