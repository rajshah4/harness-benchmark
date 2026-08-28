# Completion-Loop App Experiment

## 1. Introduction

### 1.1 Question

How much does a coding harness affect completion on a realistic application, and how much additional value comes from separating implementation, validation, and the decision to stop?

This is a workshop case study, not a tight benchmark or a universal harness leaderboard. The first run uses one application, one AWS machine, one attempt per cell, and one model. It is intended to make harness behavior inspectable and to generate hypotheses for later repeats.

### 1.2 Source idea

Factory's ProgramBench experiment compares a single agent with a three-role system. Its key intervention is not generic parallelism: a validator defines and maintains an independent standard of completion, an implementer changes the candidate, and an orchestrator decides what findings matter and when to ship. The system runs are not compute-matched and each reported cell is a single campaign.

Our application experiment keeps that causal idea but uses a normal product task rather than black-box program reimplementation. The external source of truth is the written product contract plus an instructor-owned verifier.

## 2. Workshop Learning Goals

Participants should be able to see:

1. The same model can produce different work patterns and outcomes through different harnesses.
2. “Multi-agent” is only meaningful when roles have distinct authority, context, and output contracts.
3. A system may improve quality by spending materially more time and tokens; quality, latency, and cost must be shown together.
4. Agent telemetry is useful for behavior, but provider-returned usage is the accounting source of truth.
5. A single run is a case study. Repeated randomized trials are needed before estimating an average effect.

## 3. First-Run Matrix

All cells use `openhands/claude-sonnet-4-6` through the OpenHands hosted LLM provider on the same AWS host and the same committed starter tree.

The calibrated provider request parameters must also match. For this first run, both harnesses use `reasoning_effort: high` and `max_completion_tokens: 64000`; matching the model name alone is insufficient.

| Cell | Coding harness | Roles | Stop authority |
| --- | --- | --- | --- |
| `openhands-single` | OpenHands CodeAct | one implementer | implementer |
| `pi-single` | Pi via Agent Canvas ACP | one implementer | implementer |
| `openhands-system` | OpenHands CodeAct | implementer, validator, orchestrator | orchestrator |

Every role receives the explicit 11-skill allow-list introduced by Vasco's OpenHands change. Public, user, or project skill discovery cannot silently enlarge that baseline during the measured run.

## 4. Application

Use the existing Incident Operations Center long-project task. It asks the agent to turn a small in-memory Python application into a durable full-stack operations product with SQLite persistence, concurrent alert deduplication, escalation leases, audit history, JSON HTTP routes, a responsive browser UI, and import/export commands.

This is a good first workshop application because it has:

- enough surface area for premature stopping to be plausible;
- backend, concurrency, CLI, HTTP, and frontend integration work;
- an existing committed starter tree;
- an instructor-owned verifier with behavioral checks;
- prior Sonnet and GLM runs that provide context without contaminating this run's accounting.

## 5. System Condition

### 5.1 Roles

The implementer owns the candidate workspace and performs normal development. It never receives the hidden verifier or its raw output.

The validator receives a fresh snapshot of the candidate, the public product contract, and a controller-produced outcome summary. It may inspect and execute the snapshot but cannot change the implementer's workspace. It writes a structured report that clusters failures by product capability and distinguishes blocking defects from uncertainty.

The orchestrator receives the public contract, the validator's clustered report, prior directives, and budget state. It writes a structured `STOP` or `CONTINUE` decision. For `CONTINUE`, it produces a capability-level directive for the implementer; it does not disclose hidden cases.

### 5.2 Outer loop

1. The implementer builds and self-checks the candidate.
2. The controller snapshots the candidate and runs the instructor verifier outside every agent workspace.
3. The validator inspects the snapshot and clusters the supplied outcome evidence.
4. The orchestrator adjudicates the report.
5. On `CONTINUE`, only the orchestrator's directive crosses back to the implementer.
6. Repeat until `STOP` or the declared round cap.
7. Run the instructor verifier once more against the shipped candidate.

The controller is deterministic lifecycle code. It does not decide what to implement or when the artifact is complete.

### 5.3 Information-wall limitation

Agent Canvas `LocalWorkspace` instances provide separate working directories, not a cryptographic sandbox boundary. The validator's snapshot and orchestrator workspace keep the instrument and raw evidence out of the implementer's supplied context, but this is an operational wall rather than a security boundary. A later Enterprise-container repeat can harden the boundary.

## 6. Measurement Contract

### 6.1 Hard preflight gate

Before the app run, each harness type completes a three-turn calibration. The run is blocked unless provider-boundary records prove all of the following:

- every request model is exactly `claude-sonnet-4-6`;
- each successful provider response includes raw usage;
- response IDs are present and unique;
- there are no provider or streaming errors;
- the serialized skill list is exactly the pinned 11-skill allow-list.

This prevents a profile label, UI display, or missing ACP telemetry from being mistaken for evidence of the model and usage actually consumed.

### 6.2 Primary outcomes

- instructor-verifier checks passed and final pass/fail;
- wall-clock time to ship;
- provider model calls;
- provider input, cache-read, cache-write, and output tokens;
- fresh input tokens where derivable;
- tool calls and files changed;
- repair rounds and the orchestrator's stop reason.

For the system cell, usage is also broken out by implementer, validator, and orchestrator. The final comparison shows both total system spend and implementer-only spend.

### 6.3 Evidence and privacy

The provider ledger stores request shape, hashes, timing, status, model name, response ID, and raw usage. It does not store prompts, responses, tool output, code, or credentials. Agent events remain behavioral evidence, not the token accounting authority.

## 7. Interpretation

The first run can demonstrate mechanisms and produce a useful workshop narrative. It cannot estimate variance or prove that one harness is generally better. A system win can arise from the independent completion loop, extra compute, or both; all three must be discussed together.

The next defensible step is at least three clean attempts per cell with run order randomized. A stronger causal follow-up adds an `openhands-single` condition with the same token or wall-clock budget as the system, separating “better stopping discipline” from “simply spent more.”

## 8. Go/No-Go Criteria

Proceed with the paid app run only when:

- AWS host, Agent Canvas, Agent Server, and image revisions are recorded;
- the calibration gate passes for native OpenHands and Pi;
- both starter workspaces have the same Git tree hash;
- the verifier environment passes its own smoke check;
- the provider ledger is fresh and writable;
- no prior candidate workspace will be reused.

If any criterion fails, preserve the evidence, fix the setup, create a new run ID, and recalibrate. Do not relabel a partial or misconfigured attempt as a benchmark result.
