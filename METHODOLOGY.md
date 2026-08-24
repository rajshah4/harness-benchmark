# Methodology

## Research question

When the model stays fixed, how much do coding-agent harnesses differ in correctness, time, model calls, context size, cache use, and cost?

## Controlled variables

The clean AWS experiments held these constant:

- model: GLM-5.2
- provider endpoint and pricing
- machine and software environment
- task prompt
- starting repository tree
- permissions
- timeout policy
- external verifier
- repair feedback: disabled

Each harness received an isolated workspace. Harness order was rotated across the short tasks.

## Independent variable

The independent variable was the coding harness:

- native OpenHands
- Pi through ACP
- OpenCode through ACP

Codex is documented separately because the available control used GPT-5.5 rather than GLM-5.2.

## Outcome measures

- **Quality:** external verifier result after the harness stopped.
- **Elapsed time:** runner-recorded wall-clock time from submission to terminal state.
- **Model calls:** successful provider responses associated with the primary attempt.
- **Input tokens:** all context processed by the provider, including cached input.
- **Cache reads:** input tokens reported by the provider as reused from cache.
- **Fresh input:** input tokens minus cache reads.
- **Output tokens:** provider-reported generated tokens.
- **Cost:** calculated consistently from provider-returned usage.

One model call is not one tool action. A response can request multiple actions, and a harness can call the model without invoking a tool.

## Token authority

Every request in the clean AWS experiments passed through a recording proxy. The proxy retained request metadata and the raw usage object returned by the provider. It did not retain message content.

Provider usage is authoritative for cross-harness token and cost comparisons. Laminar traces are useful for reconstructing the action sequence, but did not reconcile consistently across all harnesses. In particular, OpenHands traces reported zero cache reads when the provider reported substantial cache use.

## Calibration gates

Before running benchmark tasks:

1. Send controlled prompts through every harness.
2. Confirm that all harnesses resolve to the intended model and endpoint.
3. Confirm one ledger record for every successful provider response.
4. Confirm that prompt, output, cache-read, and cost fields use the same definitions.
5. Confirm that run, task, harness, and phase labels survive the full path.
6. Confirm that a deliberately repeated prefix produces provider cache reads.

If usage coverage is incomplete, publish token fields as unavailable. Do not substitute interface counters or report missing telemetry as zero.

## Quality protocol

The agent does not receive the external verifier. After the primary attempt ends, the runner executes the verifier against the resulting workspace. Repair feedback is disabled in the published runs.

Verifier failures must be audited. The full-stack incident verifier initially contained two invalid assumptions. The raw scores are preserved, and the corrected behavioral interpretation is documented in the result report.

## Reproduction

The original experiments used Agent Canvas 1.15.0 and OpenHands Agent Server 1.42.1 on a temporary AWS instance. Reproduction requires configured OpenHands, Pi, and OpenCode profiles that route through the recording proxy.

The reusable components are:

- `runner/provider_ledger_proxy.py`: provider-boundary usage recorder
- `runner/calibrate_ledger.py`: accounting calibration
- `runner/run_suite.py`: task execution and verification
- `runner/analyze_aws_provider_suite.py`: result aggregation
- `runner/MEASUREMENT-PROTOCOL.md`: detailed operational protocol

Profile files are intentionally omitted because they are deployment-specific and may contain authentication references. The runner expects credentials to come from environment or secret storage, never checked-in files.

## Publishability limits

The current results are a transparent case study, not a definitive leaderboard:

- one trial per task and harness
- a custom task distribution
- one shared model in the controlled lanes
- correctness measured against explicit contracts

A stronger follow-up should repeat selected tasks at least three times per harness and publish the median, range, first-pass success rate, and raw trials.

