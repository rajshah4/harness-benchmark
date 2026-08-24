# Durable Job Queue Experiment

This task applies the FeatureBench evaluation pattern to a workshop-sized repository. Agents receive an existing package, a feature specification, visible regression tests, and instructor-owned fail-to-pass checks.

The task is intentionally broader than a single algorithm. A successful solution must coordinate a persistent model, SQLite transaction behavior, execution policy, retries, crash recovery, cancellation, and a cross-process CLI.

Run it through Agent Canvas from the harness-suite directory:

```bash
python run_suite.py \
  --run-id 20260822-durable-job-queue-v1 \
  --task durable-job-queue \
  --include-codex
```
