# Commit0 compared with this benchmark

The Commit0 results do not contradict the AWS results. The two evaluations ask different questions.

Commit0 asks which harness completely reconstructs more Python libraries from stubs under a large work budget. This benchmark asks whether harnesses reach a specified outcome and how much time, context, and money they use to get there.

## Same-model Commit0 results

The OpenHands Index currently reports these fully resolved repository rates:

| Model | Harness | Score | Cost per repository | Average runtime |
| --- | --- | ---: | ---: | ---: |
| GPT-5.4 | OpenHands | 56.2% (9/16) | $4.04 | 1,173 s |
| GPT-5.4 | Codex | 50.0% (8/16) | $1.84 | 504 s |
| GPT-5.5 | OpenHands | 43.8% (7/16) | $5.56 | 1,029 s |
| GPT-5.5 | Codex | 37.5% (6/16) | $5.57 | 425 s |
| Claude Opus 4.6 | OpenHands | 56.2% (9/16) | $7.69 | 1,030 s |
| Claude Opus 4.6 | Claude Code | 37.5% (6/16) | $10.39 | 1,522 s |
| Claude Opus 4.7 | OpenHands | 56.2% (9/16) | $5.69 | 636 s |
| Claude Opus 4.7 | Claude Code | 43.8% (7/16) | $7.34 | 1,433 s |
| Claude Sonnet 4.5 | OpenHands | 12.5% (2/16) | $3.23 | 756 s |
| Claude Sonnet 4.5 | Claude Code | 31.2% (5/16) | $2.19 | 717 s |

OpenHands is not uniformly ahead. Claude Code leads the Sonnet 4.5 pair. The clearest Codex comparison is GPT-5.4: OpenHands gains one additional solved repository, while Codex is about 57 percent cheaper and 57 percent faster.

## Why the ranking can change

### 1. Quality and efficiency are different objectives

Commit0's headline metric is the percentage of repositories with every test passing. Time and cost are reported, but do not affect the score. Extra calls and extra context can be worthwhile if they turn one more repository into a complete solve.

In the AWS short suite, every harness passed 8/8. Once quality tied, the visible difference was efficiency. OpenHands used more context and cost. On Commit0 with GPT-5.4, that extra work coincided with one additional complete solve.

### 2. The task distributions are different

Commit0 contains 16 Python-library reconstruction tasks in its lite split. Agents fill many stubs and repeatedly run unit tests. The work is broad and implementation-heavy, but structurally repetitive.

The AWS suite includes focused repairs, concurrency, a security review, persistent state, background work, a CLI, HTTP behavior, browser UI, and hidden cross-process checks. The full-stack project rewards integration and careful stopping, not only broad implementation coverage.

### 3. OpenHands' thorough loop fits Commit0

OpenHands often sends more context and, on difficult tasks, makes more model calls. That hurt time and cost in the AWS experiments. On a benchmark that awards a solve only when every test passes, persistence can improve the headline score.

### 4. The adapters are not identical products

The OpenHands runner uses the native OpenHands agent directly. Claude Code and Codex run through ACP packages inside the same evaluation infrastructure. They retain their own prompts, tools, planning, and context policies, but also depend on the ACP integration and its version.

"Same model" therefore isolates the model, not every other setting. Harness prompts, tool surfaces, history management, inference defaults, and adapter versions remain part of the comparison.

### 5. The sample is small enough that one repository moves the score

Commit0 lite has 16 repositories. One additional complete solve changes the score by 6.25 percentage points. The GPT-5.4 OpenHands versus Codex gap is exactly one repository.

## Practical interpretation

The joint finding is more useful than either leaderboard alone:

> OpenHands' heavier loop can buy additional task completion on some long, test-driven implementation work. On tasks where multiple harnesses already reach the required outcome, the same loop can be slower and more expensive.

This is why a harness benchmark should report quality, elapsed time, calls, provider tokens, cache use, and cost together.

## Primary sources

- [Commit0 benchmark repository](https://github.com/commit-0/commit0)
- [OpenHands Commit0 runner and methodology](https://github.com/OpenHands/benchmarks/tree/main/benchmarks/commit0)
- [OpenHands GPT-5.4 results](https://github.com/OpenHands/openhands-index-results/blob/main/results/GPT-5.4/scores.json)
- [Codex GPT-5.4 results](https://github.com/OpenHands/openhands-index-results/blob/main/alternative_agents/acp-codex/GPT-5.4/scores.json)
- [OpenHands GPT-5.5 results](https://github.com/OpenHands/openhands-index-results/blob/main/results/GPT-5.5/scores.json)
- [Codex GPT-5.5 results](https://github.com/OpenHands/openhands-index-results/blob/main/alternative_agents/acp-codex/GPT-5.5/scores.json)
- [OpenHands and Claude model results](https://github.com/OpenHands/openhands-index-results/tree/main/results)
- [Claude Code ACP results](https://github.com/OpenHands/openhands-index-results/tree/main/alternative_agents/acp-claude)

