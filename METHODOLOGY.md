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

## Current-main follow-up matrix

The August 24, 2026 follow-up expands the independent variables to four
harnesses and three models:

- harnesses: native OpenHands, Pi ACP, OpenCode ACP, and Codex ACP
- models: GLM-5.2, DeepSeek V4 Pro, and Claude Sonnet 4.5

It uses the Agent Canvas source at merge commit
`c4c5bb74679a9cdab8ea7e863ff491c79f9cdbc0`, which includes
OpenHands/OpenHands#16860 and `@openhands/extensions` 0.18.0. That release
defines 11 recommended default skills. Saved Agent Profiles on the benchmark
host materialized an unequal skill context (60 skills for native OpenHands and
zero Canvas skills for ACP profiles), so the runner uses the public
conversation API with inline agent settings and injects the exact same 11
serialized skills into every cell. The result artifact records the names,
count, and source. This controls the released default-skill change without
silently comparing unequal profile behavior.

Every one of the 12 model-harness lanes first ran the same one-minute external
verifier calibration. The long matrix did not start until every lane:

1. passed the verifier on its first attempt;
2. produced one provider-ledger row per provider response;
3. returned input, output, and cache-status fields from the provider; and
4. stored exactly the pinned 11-skill context.

The long cells run sequentially, use fresh workspaces, receive no verifier
feedback, and allow no repair round. Codex uses its supported custom-provider
configuration and Responses wire protocol, routed through the same recording
proxy as the other harnesses.

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

The original experiments used Agent Canvas 1.15.0 and OpenHands Agent Server 1.42.1 on a temporary AWS instance. The current-main follow-up pins the source
commit and equalized skill set above while retaining the same isolated AWS
host and Agent Server API. Reproduction requires profiles that route through
the recording proxy.

The reusable components are:

- `runner/provider_ledger_proxy.py`: provider-boundary usage recorder
- `runner/calibrate_ledger.py`: accounting calibration
- `runner/run_suite.py`: task execution and verification
- `runner/analyze_aws_provider_suite.py`: result aggregation
- `runner/configs/`: secret-free Pi, OpenCode, and Codex provider configs
- `runner/configure_*_matrix.py`: profile setup through Agent Canvas APIs
- `runner/run_current_main_long_matrix.py`: resumable sequential matrix runner
- `runner/MEASUREMENT-PROTOCOL.md`: detailed operational protocol

Checked-in configuration files contain routes and environment-variable
references only. The runner expects credentials to come from the environment
or Agent Canvas secret storage; keys are never written to result artifacts or
provider ledgers.

## Publishability limits

The current results are a transparent case study, not a definitive leaderboard:

- one trial per task and harness
- a custom task distribution
- one shared model in the controlled lanes
- correctness measured against explicit contracts

A stronger follow-up should repeat selected tasks at least three times per harness and publish the median, range, first-pass success rate, and raw trials.
