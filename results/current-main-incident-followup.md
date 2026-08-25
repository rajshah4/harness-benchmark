# Current-main incident follow-up

This follow-up ran the Incident Operations Center task after the current
OpenHands main defaults reduced the default skill set to 11. Each GLM-5.2 lane
used a fresh AWS workspace, the same task and external verifier, no repair
round, and the same injected 11-skill context. Provider-boundary records are
the token and cost authority for OpenHands, Pi, and OpenCode.

| Harness | Quality | Time | Provider calls | Tool calls | Input | Fresh input | Cache rate | Output | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OpenHands | 8/8 | 8m 38s | 49 | 48 | 2.19M | 151K | 93.1% | 34.4K | $0.536 |
| Pi | 8/8 | 15m 41s | 69 | 79 | 3.23M | 199K | 93.8% | 58.3K | $1.093 |
| OpenCode | 7/8 | 13m 04s | 50 | 56 | 1.96M | 59.9K | 96.9% | 41.1K | $0.615 |
| Codex | 8/8 | 33m 06s | 168 | 160 | 10.93M | 214K | 98.0% | 76.5K | $2.784 |

The change produced a materially shorter OpenHands GLM trajectory than the
earlier incident trials. One run does not establish causality or stability, so
this is a follow-up result rather than a replacement leaderboard. OpenCode's
run missed one external check.

Codex used the Responses route, which names its fields `input_tokens` and
`output_tokens` rather than the chat-completions names used by the other
lanes. The provider-boundary ledger records both shapes. The initial Canvas
summary omitted those fields, but the public ledger and this table use the
provider values.

## DeepSeek V4 Pro compatibility check

We stopped the DeepSeek portion after two different harnesses failed to make a
valid completion. This is a compatibility outcome, not a performance result.

| Harness | Status | Time before stop | Tool calls | Verifier |
| --- | --- | ---: | ---: | ---: |
| OpenHands | `stuck` | 10m 27s | 2 | fail |
| Pi | `aborted` after no progress | 37m 34s | 48 | fail |

OpenHands produced a planning response without a tool call and Canvas marked
the conversation stuck. Pi ceased emitting events and provider requests while
its conversation remained running, so it was interrupted. The unfinished
OpenCode and Codex lanes were not run.

## Evidence

- [`current-main-matrix-in-progress.jsonl`](provider-ledgers/current-main-matrix-in-progress.jsonl): provider-boundary records, request hashes, usage, cache fields, latency, and error classifications.
- [`traces/`](traces/): sanitized event streams, including all four GLM runs and both DeepSeek failures.
- [`current-main-calibration.md`](current-main-calibration.md): per-lane three-turn accounting calibration before the long task.
