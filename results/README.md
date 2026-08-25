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

The [`incident-sonnet-harness-comparison.md`](incident-sonnet-harness-comparison.md)
report compares Sonnet across OpenHands, Pi, and OpenCode, then isolates Pi's
missing Sonnet cache markers with a cache-enabled repeat.

The [`browser-tool-impact.md`](browser-tool-impact.md) report traces how the
browser tool drove a large share of the token, time, and correctness
differences across the GLM-5.2, Sonnet, OpenCode, and current-main
conditions on the incident project.

The [`browser-tool-audit.md`](browser-tool-audit.md) report documents that
neither medium task required a browser, with a trace-by-trace audit showing
browser tool declaration versus actual usage.

The [`medium-project-token-differences.md`](medium-project-token-differences.md)
report explains the 4x token gap between Pi and OpenHands on the two medium
projects, using the provider ledger's per-call `tool_count` and prompt
tokens to decompose the difference into tool-count-driven per-call prompt
size and call-count-driven total volume.

The [`traces/`](traces/) directory contains complete sanitized Canvas event streams for the accepted long-project runs. It omits deployment metadata and invalid attempts. See its README and manifest for the exact inclusion and redaction rules.
