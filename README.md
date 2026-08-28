# Same Model, Different Coding Harnesses

This repository contains a reproducible comparison of coding-agent harnesses. The central experiment holds the model, task, starting files, machine, and verifier constant, then compares how OpenHands, Pi, and OpenCode use the model.

The primary result is not a universal leaderboard. It is a demonstration that harness design changes correctness, time, model calls, context size, cache use, and cost even when the underlying model is identical.

See [`BENCHMARK_RESULTS.md`](BENCHMARK_RESULTS.md) for the consolidated single-agent and multi-agent findings, including the CSVQ and freight control tower experiments.

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

### Why Pi used fewer tokens on medium projects

Pi used roughly half the tokens of OpenCode and a quarter of OpenHands on
the two medium projects. The provider ledger shows the cause: each harness
declares a different number of tools to the model on every call, and that
drives the per-call prompt size.

| Harness | Tools per call | Avg prompt | Calls (spread-plate) | Total tokens |
| --- | ---: | ---: | ---: | ---: |
| Pi | 4 | 10,596 | 24 | 254,314 |
| OpenCode | 10 | 18,788 | 42 | 789,088 |
| OpenHands | 22 (14 browser) | 30,249 | 63 | 1,905,661 |

Each tool schema costs roughly 1,000 to 1,400 prompt tokens. Pi's 4-tool
harness sends the smallest prompt; OpenHands' 22-tool harness (including
14 browser functions) sends the largest. Neither medium task required the
agent to use a browser — Durable Job Queue is pure backend, and Spread
Plate is a static web app the agent builds by writing files (its verifier
uses Playwright, but the agent does not). OpenHands made zero browser
actions on Durable Job Queue and one on Spread Plate, yet paid for 14
browser schemas on all 98 calls. Pi also makes fewer calls because
it packs multiple commands into single bash invocations, where OpenHands
uses one model call per command. The two factors compound: 2.6x more calls
times 2.9x larger prompts produces the 7.5x total-token gap. Caching helps
all three harnesses similarly and cannot close it. The deeper analysis,
including why the ordering inverts on the long project, is in
[`results/medium-project-token-differences.md`](results/medium-project-token-differences.md).

### Full-stack incident project

| Harness | Contract-adjusted quality | Model calls | Input tokens | Time | Provider cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenCode | 8/8 | 76 | 2,746,582 | 17m 40s | $0.768 |
| Pi | 7/8 | 69 | 3,164,231 | 20m 21s | $1.054 |
| OpenHands | Specified behavior passed | 95 | 6,759,904 | 26m 42s | $2.614 |

The long project shows why call count alone is not enough. Pi made fewer calls than OpenCode, but sent more context on each call and finished with higher token use and cost.

### Same full-stack project with Sonnet 4.5

We repeated the incident project with Sonnet to test whether the GLM-5.2
pattern was a property of the harness alone. It was not.

| Harness | Quality | Time | Model calls | Tool calls | Input | Cached | Fresh input | Output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OpenHands | 7/8 | 12m 24s | 66 | 65 | 4.26M | 97.7% | 98K | 42.5K |
| Pi, cache enabled | 6/8 | 16m 49s | 89 | 88 | 3.84M | 94.5% | 213K | 58.3K |
| OpenCode | 6/8 | 15m 52s | 75 | 82 | 4.15M | 97.9% | 87K | 82.6K |

The ordering changed with the model. OpenHands needed 95 to 129 calls in its
two GLM-5.2 trials, but only 66 with Sonnet. Pi moved in the other direction:
69 calls with GLM-5.2 and 89 with Sonnet. OpenCode was nearly unchanged at 76
and 75 calls. This points to a model-and-harness interaction, not a universal
ranking of harnesses. Sonnet appears to be a particularly good match for how
OpenHands structures its instructions, tools, and loop, although its shorter
path still missed one frontend defect.

## The browser tool and token usage

A large share of the token, time, and correctness differences above comes
down to one harness-level choice: whether the browser tool is in the prompt
at all. The full-stack incident project requires a browser interface, so it
isolates this effect cleanly. The same pattern appears across four
conditions:

| Condition | Declared tools | Browser actions | Input tokens | Time |
| --- | ---: | ---: | ---: | ---: |
| OpenHands + GLM-5.2, original | 22 (15 browser) | 17, mostly failing | 6.76M | ~26.7 min |
| OpenHands + Sonnet 4.5 | 21 (14 browser) | 0, never invoked | 4.26M | ~12.4 min |
| OpenCode + GLM-5.2 | ~5, no browser | 0 | 2.75M | ~17.7 min |
| OpenHands + GLM-5.2, current-main | 9, browser removed | 0 | see ledger | ~8.6 min |

What happened in each condition:

- **GLM-5.2, originally**, OpenHands declared a 14-function browser toolset in
  every prompt and then the browser mostly failed at runtime: cold-start
  timeouts and extraction errors pushed the agent into retry loops. The
  browser consumed ~21 percent of wall-clock and most of it failed against
  the tool, not the application.
- **With Sonnet 4.5**, the same browser schemas sat in every prompt but the
  model never invoked them. Sonnet substituted a shallow `curl | grep`
  presence check, ran in half the time and half the tokens, but missed one
  frontend check the browser would have caught.
- **OpenCode** never declared a browser at all. Its five-tool harness carried
  no browser overhead, and it verified the frontend with per-marker
  completeness checks in inline Python. It finished 8/8 at 2.75M input tokens.
- **When the browser tool was removed** (current-main), OpenHands + GLM-5.2
  converged on the same no-browser verification pattern OpenCode used, and
  ran in roughly a third of the original time.

The practical reading: a tool is not just a capability the agent may use. It
is also a fixed per-call prompt cost paid on every turn, and if the tool is
unreliable, a behavioral cost paid in retries. On this task, for OpenHands,
the browser was a net cost on both axes, not a net capability. The deeper
analysis, with trace references and the test-file-edit caveat for OpenCode,
is in [`results/browser-tool-impact.md`](results/browser-tool-impact.md).

Pi initially received zero cache reads because its Sonnet requests did not
include Anthropic-style cache controls. The table uses the controlled Pi
rerun with those controls enabled. Its cache rate reached 94.5 percent, but it
still made 89 calls. Caching reduced fresh input, not the agent loop. See the
[`Sonnet harness comparison`](results/incident-sonnet-harness-comparison.md)
for the controlled cache repeat and sanitized evidence. The provider did not
return comparable Sonnet costs, so they are not shown here.

### Current-main GLM-5.2 follow-up

After OpenHands main reduced the default skill set to 11, we repeated the
incident project with the same curated skill context across OpenHands, Pi,
OpenCode, and Codex. OpenHands completed 8/8 in 8m 38s with 49 provider calls.
Pi also completed 8/8 in 15m 41s with 69 calls. OpenCode finished in 13m 04s
but missed one check. Codex completed 8/8 in 33m 06s with 168 calls and 10.93
million provider input tokens. The full result and the DeepSeek compatibility
failures are in the
[`current-main incident follow-up`](results/current-main-incident-followup.md).

## What is here

- [`METHODOLOGY.md`](METHODOLOGY.md): experimental controls, token definitions, limitations, and reproduction procedure.
- [`BENCHMARK_RESULTS.md`](BENCHMARK_RESULTS.md): consolidated harness × agent-architecture results.
- [`docs/acp-harness-integration.md`](docs/acp-harness-integration.md): how Pi and OpenCode were added as custom ACP harnesses.
- [`REPLICATION-CAVEATS.md`](REPLICATION-CAVEATS.md): practical failure modes and checks for reproducing the benchmark.
- [`benchmark/`](benchmark/): prompts, starting repositories, and external verifiers.
- [`runner/`](runner/): Agent Canvas runner, provider-boundary ledger, calibration checks, and tests.
- [`results/short-suite.md`](results/short-suite.md): all eight short tasks.
- [`results/long-projects.md`](results/long-projects.md): the spread-plate application and durable job queue.
- [`results/spread-plate-repeat-trial.md`](results/spread-plate-repeat-trial.md): a second OpenHands spread-plate trial, including provider stalls and agent-loop variance.
- [`results/incident-project.md`](results/incident-project.md): the full-stack incident project and verifier audit.
- [`results/incident-repeat-trials.md`](results/incident-repeat-trials.md): repeated OpenHands GLM and Sonnet incident runs.
- [`results/incident-sonnet-harness-comparison.md`](results/incident-sonnet-harness-comparison.md): Sonnet across harnesses and the controlled Pi cache-marker experiment.
- [`results/current-main-calibration.md`](results/current-main-calibration.md): the 12-lane accounting and quality gate for the current-main, three-model follow-up.
- [`results/current-main-incident-followup.md`](results/current-main-incident-followup.md): the current-main GLM incident results, DeepSeek compatibility failures, and evidence links.
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
