# Harness and Multi-Agent Benchmark Design

## 1. Introduction

### 1.1 Problem Statement

Factory Research demonstrated that multi-role agent systems dramatically outperform single agents on large software tasks, with the key driver being an independent standard of completion held behind an information wall from the implementer. However, their experiment used a single harness (Droid) across all conditions, leaving open whether the harness itself — the agent loop, tool set, and orchestration pattern — affects the outcome when the underlying model is held constant.

We cannot assess the harness contribution from Factory's data because only one harness was used. If we want to make claims about which harness characteristics matter for single-agent or multi-agent performance, we need a controlled comparison that varies the harness while holding the model and task constant.

### 1.2 Proposed Solution

We propose a 2×2 factorial benchmark crossing two harnesses with two conditions, all using the same model (GLM 5.2), on a set of tractable greenfield application tasks.

The beneficiary of the design — the person reading the results — experiences the outcome as a comparison matrix: for each task, four scores (harness × condition), making visible both the harness effect and the multi-agent effect. The design isolates the harness as the single variable by holding model, task, spec, fixtures, hidden test suite, sandbox size, and stopping rule constant.

A limitation is that our tasks are much smaller than Factory's ProgramBench tasks (gdal, 7zip, etc.). Our tasks are completable in 1–4 hours rather than 15–196 hours. This trades ecological validity for tractability and repeatability. We chose this because the research question is about relative harness performance, not absolute capability, and a relative comparison is valid on smaller tasks if the tasks still have enough behavioral surface for a validator to build a meaningful instrument. A second limitation is one campaign per cell (no variance estimate), matching Factory's own methodology.

## 2. New Concepts

### 2.1 The Four-Cell Design

Each task is run under four conditions:

| | Single-agent | Multi-agent (system) |
|---|---|---|
| **OpenHands native** | A1: one CodeAct conversation | B1: orchestrator + implementer + validator, all OpenHands |
| **PI via ACP** | A2: one PI conversation | B2: orchestrator + implementer + validator, all PI |

The underlying model is GLM 5.2 in every cell. The reasoning level is held constant across all roles and conditions.

### 2.2 The Information Wall

The multi-agent condition implements Factory's "wall" using OpenHands Enterprise conversation boundaries:

- **Validator** holds the instrument (a weighted body of test cases) in its LLM context. It runs the candidate against those cases and reports clustered findings to the orchestrator. The instrument never touches the shared filesystem.
- **Orchestrator** adjudicates findings and sends directives to the implementer. Directives describe missing features or behaviors at the level of subsystems, not individual test cases.
- **Implementer** receives directives, investigates the reference binary independently, and advances the candidate. It never sees the instrument, its cases, or raw output.
- **Hidden test suite** lives entirely outside all agent conversations. It is run once by an external evaluator after the campaign ends.

### 2.3 The Shared Sandbox

All three roles in a system run share one 8 GiB sandbox via explicit `sandbox_id` attachment (the pattern from the user's `openhands-multi-agent-demo` repository). This gives three separate conversation histories — so the information wall holds at the context level — while consuming one sandbox allocation.

## 3. Technical Design

### 4.1 Task Suite

#### 4.1.1 Task Selection Criteria

Each task must:

1. Have a clear, testable behavioral surface (CLI output, exit codes, file outputs, or HTTP responses)
2. Be completable in 1–4 hours by a competent agent
3. Have enough surface that a validator can build a meaningful weighted instrument (hundreds of cases)
4. Be greenfield — no dependency on existing codebases the agent might have seen

#### 4.1.2 Tasks

Three CLI tools, each with a reference implementation (the oracle), a partial spec, and a hidden test suite:

**T1: CSV processor (`csvq`)**

Subcommands: `select`, `filter`, `sort`, `stats`, `join`. Reads CSV from files or stdin, writes to stdout. Clear input→output surface. Behavioral surface includes: header handling, quoting, type inference, aggregation functions, multi-file join.

**T2: Markdown-to-HTML converter (`md2html`)**

Parses CommonMark subset, emits HTML. Flags for: `--full-document` (wrap in `<html>`), `--no-styles`, `--toc` (generate table of contents). Behavioral surface includes: headings, lists, code blocks, tables, nested emphasis, link references.

**T3: JSON query tool (`jq-lite`)**

Path expressions, filter, select, map. Reads JSON from stdin or files. Behavioral surface includes: identity, index, slice, iterate, select with comparisons, pipe, length, keys, values.

#### 4.1.3 Task Artifacts

For each task, we author before any campaign:

```text
tasks/<task-name>/
  spec.md              # partial documentation (what the agent receives)
  oracle/              # reference implementation (hidden source)
    src/
    Cargo.toml         # or package.json, pyproject.toml
  oracle-bin           # compiled binary (the black-box oracle the agent can run)
  fixtures/            # sample input files
  hidden-suite/        # the grading suite (never visible to agents)
    cases/
    expected/          # expected output per case (generated by running oracle)
    manifest.json       # case list, weights, comparison rules
```

The agent receives: `spec.md`, `fixtures/`, and the ability to run `./oracle-bin <args>`. It never receives: `oracle/` source, `hidden-suite/`.

### 4.2 Conditions

#### 4.2.1 Single-Agent Condition (A1, A2)

One conversation, one agent (OpenHands or PI), one sandbox. The agent:

1. Reads `spec.md`
2. Investigates the oracle binary with its own probes
3. Implements the candidate
4. Runs its own checks
5. Decides when it is done

The agent's own checks develop inside the same context as the candidate — there is no independent instrument. The agent ships when it judges its work complete.

#### 4.2.2 Multi-Agent System Condition (B1, B2)

Three conversations sharing one sandbox, plus an external evaluator:

1. **Validator** surveys the oracle binary, builds the instrument (weighted test body) in its context, and measures successive candidates.
2. **Orchestrator** receives clustered findings, adjudicates, and sends directives to the implementer.
3. **Implementer** receives directives, investigates the oracle independently, and advances the candidate code.
4. **External evaluator** runs the hidden suite once after the orchestrator decides to ship.

The loop:

```text
validator builds instrument
  ↓
validator measures current candidate → clustered findings
  ↓
orchestrator adjudicates → directive
  ↓
implementer advances candidate
  ↓
(repeat until orchestrator ships)
  ↓
external evaluator runs hidden suite → final score
```

### 4.3 The Wall in a Shared Sandbox

Because the shared sandbox has a shared filesystem, the information wall is enforced at the conversation-context level, not the filesystem level:

- The validator's instrument lives in its LLM context. It may write test runners to `/tmp/validator/`, but the implementer is instructed (and the orchestrator enforces) not to read that path.
- Clustered findings cross the wall as messages: the validator tells the orchestrator "the `--toc` flag produces incorrect anchor links for nested headings" — not "case #47 expected `<h2 id="nested">` but got `<h2 id="Nested">`."
- The orchestrator's directive to the implementer names the missing or broken behavior and its weight, not the specific cases.

This matches Factory's design: "the candidate and the findings cross the wall; the instrument does not."

### 4.4 Stopping Rule

Each campaign runs until one of:

1. **Voluntary stop**: the single agent (A1/A2) or orchestrator (B1/B2) emits `SHIP` in its final response.
2. **Iteration cap**: 500 iterations per conversation (OpenHands `max_iterations`).
3. **Wall-clock cap**: 4 hours per campaign.

We record which condition triggered the stop, following Factory's insight that agents stop because they decide to, not because they run out of budget.

### 4.5 Grading

The hidden suite is run once by an external evaluator (a script or a separate evaluator conversation in a separate sandbox) after the campaign ends. The grade is the percentage of hidden-suite cases that pass.

Comparison rules (per Factory's approach):

- Primary: exit code match
- Secondary: stdout byte-comparison (with optional normalizers for timestamps)
- Tertiary: file output byte-comparison

The hidden suite is identical across all four cells for a given task.

### 4.6 Harness Configuration

#### 4.6.1 OpenHands Native (A1, B1)

```text
agent_profile: OpenHands-GLM-5.2 (native CodeAct)
model: openhands/glm-5.2
reasoning_effort: high
max_iterations: 500
sandbox: 8 GiB
```

For B1, three conversations share one sandbox via `sandbox_id` attachment. Each conversation uses the same OpenHands-GLM-5.2 profile.

#### 4.6.2 PI via ACP (A2, B2)

```text
agent_profile: Pi-GLM-5-2-Smoke (ACP-backed)
model: openhands/glm-5.2 via PI's openai-completions provider
reasoning_effort: high
acp_prompt_timeout: 600
sandbox: 8 GiB
```

For B2, three conversations share one sandbox. All three use the Pi-GLM-5-2-Smoke profile.

### 4.7 Orchestrator Protocol

The orchestrator is a conversation (OpenHands or PI, matching the cell's harness) that:

1. Starts the validator conversation with the spec and oracle path.
2. Waits for the validator to report the instrument is ready.
3. Starts (or resumes) the implementer conversation with the spec and directive.
4. Polls both conversations until terminal state.
5. Parses the validator's clustered findings (structured JSON).
6. Adjudicates: rejects noise, identifies real failures, constructs a directive.
7. Sends the directive to the implementer.
8. Repeats until the validator reports convergence (no meaningful new failures) or the iteration/wall-clock cap is hit.
9. Emits `SHIP` with the final candidate path.

The orchestrator runs outside the shared sandbox (or in a small 2 GiB sandbox) to avoid consuming the 8 GiB allocation.

## 5. Implementation Plan

Acceptance criteria: all task artifacts authored and oracle binaries built; hidden suites validated against the oracle; all four cells runnable end-to-end on one task; results recorded with conversation IDs, sandbox IDs, wall-clock time, iteration counts, and final scores.

### 5.1 Task Artifacts (M1)

Author specs, reference implementations, and hidden suites for T1 (CSV), T2 (Markdown), T3 (JSON query). Build oracle binaries. Validate hidden suites against oracles.

Files: `tasks/csvq/`, `tasks/md2html/`, `tasks/jq-lite/`

### 5.2 Single-Agent Harness (M2)

Implement the controller script that launches one conversation (OpenHands or PI), passes the spec and oracle, polls to terminal state, and records the candidate. Run A1 and A2 on T1. Demo: two single-agent scores for the CSV task.

Files: `controller/single_agent.py`, `controller/enterprise_api.py`

### 5.3 Multi-Agent Harness (M3)

Implement the orchestrator controller: creates shared sandbox, starts validator and implementer conversations with `sandbox_id` attachment, runs the adjudication loop, and records directives and findings. Run B1 and B2 on T1. Demo: two multi-agent scores for the CSV task, with the information wall verified.

Files: `controller/orchestrator.py`, `controller/validator_prompt.md`, `controller/implementer_prompt.md`, `controller/orchestrator_prompt.md`

### 5.4 External Evaluator (M4)

Implement the hidden-suite grader: takes a candidate binary path, runs all hidden-suite cases, byte-compares outputs, and emits a score. Run on all four cells for T1. Demo: four scores for the CSV task in a comparison matrix.

Files: `controller/evaluator.py`

### 5.5 Full Benchmark (M5)

Run all four cells on T2 and T3. Collect scores, wall-clock times, iteration counts, and stopping conditions. Produce the results matrix.

### 5.6 Analysis (M6)

Produce the 2×2 comparison matrix per task. Analyze: harness effect (OpenHands vs PI), multi-agent effect (single vs system), and interaction. Record whether single agents stopped voluntarily or hit caps. Compare to Factory's finding that the multi-agent effect dominates.

Files: `analysis/results.md`, `analysis/matrix.csv`
