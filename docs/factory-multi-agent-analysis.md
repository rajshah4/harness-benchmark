# Analysis of Factory's "What it Takes for Coding Agents to Complete Large Software Tasks"

Source: <https://factory.ai/news/what-it-takes-for-coding-agents-to-complete-large-software-tasks>
Author: Factory Research, Theo Luan — August 27, 2026

## 1. What the article is actually claiming

The central claim is narrow and worth restating precisely: **the same underlying model, held to a standard of completion it constructed before implementation, reproduces far more of a reference program's behavior than the same model running as a single agent that validates its own work as it goes.**

The evidence is a paired comparison across 24 tasks from ProgramBench, run with three models (Fable 5, Kimi K3, GPT 5.6 Sol). The headline numbers are per-task deltas: gdal 36% → 90%, 7-Zip 54% → 95%, DuckDB 34% → 80%, ctags 28% → 76%. The median single-agent score across the 24 tasks was 56.7%; the median system score was 89.3%. The gap closed 73% of the way to perfect.

The important qualifier: the model did not change between conditions. What changed was the workflow around it.

## 2. The benchmark substrate: ProgramBench

ProgramBench is a cleanroom reimplementation benchmark. Each task provides:

- A **reference program** (the oracle) that is runnable but never readable, decompilable, or traceable.
- **Fixtures** (sample inputs).
- **Partial documentation** that covers a slice of the interface.
- A **hidden grading suite** of behavioral checks that no role ever sees during the run.

The agent's job is to reproduce the observable behavior of the reference from scratch. The score is the fraction of the hidden suite that the candidate passes. The hidden suite is graded once, after the run, by the benchmark — not by any role in the system.

This is a black-box oracle design. The difficulty is that any single behavior is trivially verifiable (run oracle, run candidate, diff), but completeness of the whole is not, because the behavior space is much larger than the shipped documentation covers. The agent must discover the behavior space by probing the oracle.

## 3. The two conditions

### 3.1 Single agent

The single agent implements, checks its own work, and decides for itself when it is done. It can run the oracle without limit. It writes tests as it goes, validates each piece in the context that produced it, and eventually stops.

The article's diagnosis of why this stops early is the most important paragraph in the piece:

> An agent can therefore make steady, locally correct progress and stop with much of the outcome absent. The problem is not necessarily that it could not implement the rest. It never established a complete account of what remained.

The agent stops not because it ran out of budget, but because its self-generated evidence only covers what it thought to build. The checks inherit the scope of the work that produced them.

### 3.2 Multi-role system

The system consists of three roles, all the same model at the same reasoning level:

1. **Validator** — Before implementation, surveys the reference program and builds an "instrument": a weighted sample of the behavior space, plus the rules for a match. The instrument is the system's account of what the reimplementation must do and what evidence would prove it.
2. **Orchestrator** — Adjudicates each measurement. Decides what to measure, reviews the validator's findings, rejects noise, and turns real problems into a directive for the implementer.
3. **Implementer** — Receives directives, investigates the reference independently, and advances the candidate. **Never sees the instrument** — not its cases, not its raw output.

The loop:
1. Orchestrator chooses what should be measured.
2. Validator tests the current candidate against the instrument and clusters failures by root cause.
3. Orchestrator reviews findings, rejects invalid measurements, produces a directive at the level of missing features/subsystems/behavior.
4. Implementer investigates the reference and advances the candidate.
5. When the instrument stops revealing meaningful differences, the orchestrator can ask the validator to expand a weak area or begin targeted differential testing.

The key boundary is "the wall": the instrument lives on the measuring side. The candidate and the findings cross the wall; the instrument does not. The implementer never authors, runs, or sees the instrument. The boundary holds in both directions — the validator can expand the instrument as it learns, but cannot weaken it to accommodate what the candidate happens to contain.

## 4. Methodology assessment

### 4.1 What is solid

- **Black-box oracle with hidden grading suite.** This is the right substrate. The agent cannot memorize the answer; it must discover behavior. The hidden suite is graded by the benchmark, not by any role, so the system's own instrument is not the measure of success — it is only the system's internal proxy. This avoids the trap of "the agent grades itself and reports a high score."
- **Same model in both conditions.** The comparison isolates the workflow variable. If the system outperforms the single agent with the same model, the gain is attributable to the workflow, not the model.
- **Per-task deltas, not just averages.** Reporting every task's single-agent and system score (with the delta) lets you see that the gain is not driven by a few outliers. The gain is positive on all 24 tasks.
- **"Gap closed" framing.** The median 56.7 → 89.3 is reported as "73% of the gap closed to perfect," which is a more honest framing than raw delta because it accounts for the ceiling.

### 4.2 What is weak or underspecified

- **One campaign run per condition per model.** The article says "one campaign run" in the results panel. There is no variance, no repeated runs, no confidence intervals. With N=1 per cell, we cannot distinguish a real effect from run-to-run variance. Coding-agent runs are notoriously high-variance; a single run is a data point, not a result.
- **The "frontier" chart conflates two things.** The "single-agent frontier" is the best single-agent score across all public leaderboard entries plus Factory's own singles. The "system frontier" is the best system score. These are maxes, not means. The frontier chart shows that the system beats the single-agent max on every task, which is stronger than beating the mean — but it also means the single-agent bar is the best anyone has done, which may include other systems/workflows, not just bare single-agent runs.
- **The instrument is not independent of the model.** The validator builds the instrument by probing the same oracle the implementer probes. The instrument is the model's own account of the behavior space. If the model systematically underexplores a region of the behavior space, both the instrument and the implementation will miss it, and the hidden suite (which is independent) will catch it. The article acknowledges this: "Every score here comes from the benchmark's hidden suite — a sample of the same behavior space that no role ever saw." This is the correct control, but it means the system's internal score (instrument pass rate) can diverge from the graded score (hidden suite), and the article reports both ("official" vs "raw").
- **No cost/time normalization across conditions.** The single-agent run took 8.5h; the system run took 96h. That is an 11x cost difference. The article reports this honestly but does not normalize for it. A fair comparison would be: give the single agent 96h and see how far it gets. The article's claim is that the single agent stops early not because of budget but because of self-assessed completion — but a single agent given 96h might also keep going if it had a reason to. The system's structure gives it a reason to (the orchestrator keeps issuing directives); the single agent's structure does not.
- **The "wall" is enforced by prompt, not by technical isolation.** The implementer "never sees the instrument" because the workflow does not pass it. But all three roles are the same model. If the model has the instrument in context in one role, the same model in another role has the same knowledge. The wall is a workflow convention, not an information-theoretic boundary. In practice this is probably fine because the contexts are separate conversations, but it is worth noting.
- **Three models, but two are substituted.** The results show "Opus substitute (fable safety-killed / compile-failed)" on several tasks. This means the Fable 5 runs fell back to Opus for some tasks. This muddies the per-model comparison. The median is reported per model, but with substitutions, the "Fable 5" median includes Opus runs.

### 4.3 What we are borrowing and what we are changing

We are borrowing:
- **Black-box oracle + hidden grading suite.** Our csvq task follows this exactly: the oracle is a compiled binary, the hidden suite is 49 cases the agent never sees, and grading happens once after the run.
- **Independent grading.** Our evaluator runs after the campaign, against a suite the agent never saw. The agent's self-assessment is not the score.
- **Weighted scoring.** Our cases have weights (total 84), and the score is weighted pass rate, not raw count. This matches ProgramBench's approach.

We are changing:
- **Scale.** csvq is a small task (a CSV query CLI). The Factory tasks are large (gdal is ~600K reachable lines). Our first benchmark is deliberately small to validate the harness before scaling up. A small task will show less single-vs-system delta because the behavior space is smaller and the single agent is less likely to stop early.
- **Harness as a variable.** Factory uses Droid in both conditions. We are varying the harness: OpenHands native vs PI via ACP. This is a second variable Factory did not test. Our 2x2 is (harness) x (single vs system).
- **Model.** Factory uses three frontier models. We are using GLM 5.2 for all conditions, to isolate the harness and workflow variables from the model variable.

## 5. What the article implies for our experiment

The article's core finding — that a pre-built, independent standard of completion is the mechanism that prevents early stopping — gives us a clear hypothesis to test on a small task:

- **H1 (harness effect):** On a small task where the single agent is unlikely to stop early, the harness (OpenHands native vs PI via ACP) should have a small effect on score, because the limiting factor is the model's ability to discover and implement behavior, not the workflow.
- **H2 (workflow effect):** The multi-agent/system condition should outperform the single-agent condition, but the delta should be smaller than Factory's 73% gap-closed because our task is small and the single agent is less likely to stop early.
- **H3 (interaction):** The harness effect may interact with the workflow effect. For example, if PI's ACP harness has better tool-call structure for multi-agent coordination, the system condition may benefit more from PI than the single-agent condition does.

The small task is a deliberate tradeoff: we get a fast first data point to validate the harness, but we should expect a compressed signal. If we see a clear harness or workflow effect even on csvq, that is a strong result. If we do not, we should scale up the task before concluding the effect is absent.

## 6. Open questions for our replication

1. **How do we enforce the wall?** In our setup, the system condition will run as separate conversations (validator, orchestrator, implementer). We need to ensure the implementer conversation does not have access to the instrument. Since our instrument is the hidden suite (or a proxy the validator builds), and the hidden suite is not in the repo, this is enforced by repo structure. But if the validator builds its own instrument by probing the oracle, that instrument is text in the validator's conversation — we need to ensure it is not passed to the implementer.
2. **How do we handle the cost difference?** Factory's system run took 11x as long as the single-agent run. We should either (a) give the single agent the same budget and see if it keeps going, or (b) report the cost-normalized comparison. For the first benchmark, we will use the same max_iterations for both and report wall-clock time.
3. **What is our instrument?** Factory's validator builds an instrument by probing the oracle. In our system condition, the validator role will do the same. But our hidden suite (49 cases) is the graded measure. The validator's instrument is a separate, larger sample it constructs. We need to decide whether the validator's instrument is (a) a fresh probe of the oracle (Factory's approach) or (b) a subset of our hidden suite used as a dev set. Option (a) is more faithful to the article; option (b) is simpler but risks the instrument being too close to the graded measure.
4. **How many runs per cell?** Factory ran one campaign per cell. We should aim for at least 3 per cell to get a sense of variance, but for the first benchmark we will do 1 per cell to validate the pipeline, then repeat.
