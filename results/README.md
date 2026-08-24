# Results and evidence

The narrative reports describe the accepted experiments. The `raw` directories contain sanitized per-run records, including task prompts, timing, verifier output, model metrics, and changed-file summaries. Conversation identifiers and machine-specific paths were removed.

The provider ledgers contain one content-free record per model response. They retain the model, harness, request shape, provider token usage, cache reads, cost, response status, timestamp, and request hash. Message content and credentials were never written to these ledgers.

Raw verifier scores are preserved even when later audits found an evaluator defect. Corrections are explained in the corresponding narrative report rather than silently changing the evidence.

The [`spread-plate-repeat-trial.md`](spread-plate-repeat-trial.md) report adds a
second OpenHands trial and separates transient provider stalls from additional
agent-loop work.

The [`incident-repeat-trials.md`](incident-repeat-trials.md) report compares a
second OpenHands GLM run and an OpenHands Sonnet run with the original incident
project cells.

The [`traces/`](traces/) directory contains complete sanitized Canvas event streams for the accepted long-project runs. It omits deployment metadata and invalid attempts. See its README and manifest for the exact inclusion and redaction rules.
