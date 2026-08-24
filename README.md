# Same Model, Different Coding Harnesses

This repository contains a reproducible comparison of coding-agent harnesses. The central experiment holds the model, task, starting files, machine, and verifier constant, then compares how OpenHands, Pi, and OpenCode use the model.

The primary result is not a universal leaderboard. It is a demonstration that harness design changes correctness, time, model calls, context size, cache use, and cost even when the underlying model is identical.

## Results at a glance

All three harnesses used GLM-5.2 on a clean AWS instance.

### Eight short tasks

| Harness | Passed | Model calls | Input tokens | Context per call | Time | Provider cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Pi | 8/8 | 85 | 573,137 | 6,743 | 678.0 s | $0.3345 |
| OpenCode | 8/8 | 100 | 1,233,824 | 12,338 | 838.6 s | $0.5711 |
| OpenHands | 8/8 | 113 | 3,028,731 | 26,803 | 1,140.8 s | $1.2489 |

### Two medium projects

| Harness | Projects passed | Model calls | Input tokens | Time | Provider cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pi | 2/2 | 34 | 537,834 | 382 s | $0.222 |
| OpenCode | 2/2 | 55 | 1,270,896 | 738 s | $0.480 |
| OpenHands | 1/2 | 65 | 2,167,646 | 782 s | $0.725 |

### Full-stack incident project

| Harness | Contract-adjusted quality | Model calls | Input tokens | Time | Provider cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenCode | 8/8 | 76 | 2,746,582 | 17m 40s | $0.768 |
| Pi | 7/8 | 69 | 3,164,231 | 20m 21s | $1.054 |
| OpenHands | Specified behavior passed | 95 | 6,759,904 | 26m 42s | $2.614 |

The long project shows why call count alone is not enough. Pi made fewer calls than OpenCode, but sent more context on each call and finished with higher token use and cost.

## What is here

- [`METHODOLOGY.md`](METHODOLOGY.md): experimental controls, token definitions, limitations, and reproduction procedure.
- [`benchmark/`](benchmark/): prompts, starting repositories, and external verifiers.
- [`runner/`](runner/): Agent Canvas runner, provider-boundary ledger, calibration checks, and tests.
- [`results/short-suite.md`](results/short-suite.md): all eight short tasks.
- [`results/long-projects.md`](results/long-projects.md): the spread-plate application and durable job queue.
- [`results/incident-project.md`](results/incident-project.md): the full-stack incident project and verifier audit.
- [`results/commit0-comparison.md`](results/commit0-comparison.md): why the public Commit0 benchmark can favor OpenHands while these experiments find higher OpenHands resource use.
- [`results/codex-control.md`](results/codex-control.md): the available Codex measurements and why they are kept outside the same-model table.
- [`results/raw/`](results/raw/): sanitized per-run records.
- [`results/traces/`](results/traces/): full sanitized Agent Canvas event streams for the accepted long-project runs.
- [`results/provider-ledgers/`](results/provider-ledgers/): provider-returned usage for every measured request.

## Measurement rule

Use traces to understand what the agent did. Use provider-returned usage to compare tokens, cache reads, and cost.

Agent interfaces and tracing products do not always use the same token definitions. A missing cache field must be reported as unknown, not zero.

## Important limitations

- Each published cell was run once. Large gaps are informative, but the results are not estimates of run-to-run variance.
- A passing verifier establishes the specified behavior, not equal maintainability or production quality.
- Codex was not part of the clean AWS same-model experiment. Its available run used GPT-5.5 and a local environment.
- Commit0 measures fully solved Python-library reconstruction tasks. This repository also includes concurrency, persistent state, CLI, browser UI, and integrated full-stack work.

## Quick start

You can run any benchmark against a coding agent without the full measurement stack:

1. Give every harness its own copy of the same starter directory.
2. Send the exact task prompt from the matching benchmark directory.
3. Do not reveal or allow changes to the external verifier.
4. Run the verifier after the agent stops.
5. Record elapsed time and provider-returned usage.

For a publishable comparison, follow all calibration and accounting gates in [`METHODOLOGY.md`](METHODOLOGY.md).
