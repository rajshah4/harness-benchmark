# Caveats for replicating the harness benchmark

## Use one token authority

Agent interfaces, ACP adapters, and tracing systems did not always agree on
input tokens, cache reads, or cost. We use the raw usage returned at the shared
provider boundary for cross-harness comparisons. Native counters and Laminar
traces remain useful for reconstructing what the agent did, but they do not
overwrite provider usage.

A missing value is not zero. OpenHands once showed zero cache reads in Laminar
while the provider reported millions of cached tokens. Sonnet later returned
token usage without a cost field. We report those cases as unknown or
unavailable.

## Calibrate the complete path

Before the benchmark, send repeated prompts through every harness. Confirm the
model and endpoint, then check that every completed provider response has input,
output, cache, error, run, task, and harness fields. Include a long stable prefix
that should qualify for caching.

The calibration needs to distinguish three cases:

- positive cache reads
- a reported cache field with a real zero
- a missing cache field

Those cases have different meanings. For example, Pi with Sonnet reported a
cache field but did not engage the provider cache during calibration. That is
an adapter or request-behavior result, not missing telemetry.

## Keep the comparison controlled

Use the same task, model, provider, starting tree, permissions, timeout, repair
policy, and external verifier. Give each run a fresh workspace and conversation.
Run the harnesses sequentially so machine load and provider rate limits do not
distort wall-clock time. Rotate the execution order across repeated tasks when
possible.

Model identity alone does not guarantee parameter parity. Reasoning settings,
maximum output, tool definitions, and cache controls can differ across native
and ACP paths. If those cannot be matched, label the experiment as a comparison
of harness defaults using the same model.

The environment is part of the experiment. Our earlier local Agent Canvas setup
exposed a different tool surface, so we excluded it from the clean AWS tables.

## Separate setup and task execution

Profile loading, skill discovery, title generation, evaluator calls, and repair
rounds should not quietly enter the primary totals. Record task submission and
terminal time in the runner. Keep setup, primary work, and repairs under
separate ledger labels.

One model call is not one tool action. A model response can request several
tools, and a harness can call the model without using a tool. Keep both counts
when the trace format supports a consistent tool-action definition.

## Capture failures that look successful

Some provider failures arrive as error events inside an HTTP 200 stream. Two
such requests added about four minutes to one OpenHands repeat trial while
returning no token usage. A recorder that checks only the HTTP status will count
those as successful responses.

Save response latency, content type, size, a response hash, and a safe error
classification. Do not save the response text. When a failed request has no
usage object, the run's token and cost totals are lower bounds.

## Check quality outside the harness

Run an external verifier after the agent stops. The harness's final message and
its own tests are evidence, not the quality result. Preserve the original
verifier output and audit surprising failures. We found two mistakes in an
early incident-project verifier and documented the corrections without
rewriting the raw evidence.

The verifier also needs testing. A binary pass establishes the specified
contract, not equal maintainability or production quality.

## Repeat before ranking

Our two OpenHands trials passed the same spread-plate task but differed sharply
in time, calls, cache rate, and tokens. On the incident project, the second
OpenHands run was faster and cheaper while making more calls and sending more
context.

Publish raw trials, medians, ranges, provider failures, and first-pass success.
One run is a case study, not a stable harness ranking.

## Sanitize public evidence

Traces can contain prompts, source code, tool output, machine paths,
conversation IDs, authentication headers, and credentials. Remove deployment
metadata and secrets before publishing. Scan the exported files for common key
formats, private paths, and conversation URLs, then validate every JSON record.

The public repository includes sanitized run records, content-free provider
ledgers, and sanitized Canvas traces. Deployment profiles are omitted because
they can contain secret references and machine-specific configuration.

