# Why token usage differs on the medium projects

This note explains the 4x token gap between Pi and OpenHands on the two
medium projects. It is the deeper companion to the front-page summary in
[`README.md`](../README.md#why-pi-used-fewer-tokens-on-medium-projects).

## The question

On the two medium projects (Durable Job Queue and Artifactsbench Spread
Plate), all three harnesses used GLM-5.2 and both projects passed (OpenHands
passed one of two on the first attempt). The published totals are:

| Harness | Model calls | Input tokens | Provider cost |
| --- | ---: | ---: | ---: |
| Pi | 34 | 537,834 | $0.222 |
| OpenCode | 55 | 1,270,896 | $0.480 |
| OpenHands | 65 | 2,167,646 | $0.725 |

Pi used roughly half the tokens of OpenCode and a quarter of OpenHands. The
model, task, starting files, and machine were identical. The difference is
harness design, and it decomposes into two factors that compound each other.

## Factor one: tool count drives per-call prompt size

Each harness declares a different number of tools to the model on every call.
The provider ledger records `tool_count` per request, so we can read the
exact number rather than inferring it.

| Harness | Tools declared | Browser tools | Avg prompt per call (spread-plate) |
| --- | ---: | ---: | ---: |
| Pi | 4 | 0 | 10,596 |
| OpenCode | 10 | 0 | 18,788 |
| OpenHands | 22 | 14 | 30,249 |

The per-call prompt decomposes as a fixed base (the system prompt and task
description, roughly 5,000 to 9,000 tokens) plus the tool schemas. OpenHands
pays for 22 tool schemas on every call; Pi pays for 4. The 14 browser
schemas alone are 2,152 tokens (see the Durable Job Queue case study below);
the remaining 8 non-browser schemas are larger because they include full
JSON parameter definitions.

The browser toolset is the largest single extra payload. OpenHands declares 14
browser functions (navigate, click, get_state, get_content, type, scroll,
go_back, list_tabs, switch_tab, close_tab, get_storage, set_storage,
start_recording, stop_recording) on every call. These 14 schemas total 8,938
characters, which is 2,152 tokens under cl100k_base, and the browser usage
instructions in the system prompt add another 184 tokens. That is 2,336
tokens of fixed browser overhead per call. Neither medium task required
the agent to use a browser. Durable Job Queue is a pure backend SQLite task
with no frontend at all; its verifier has zero browser dependencies. Spread
Plate is a static web app that the agent builds by writing files, and while
its verifier uses Playwright to check the result, the agent does not need a
browser tool to build or self-verify the artifact. OpenHands made zero
browser actions on Durable Job Queue and exactly one on Spread Plate (a
single navigate to localhost for self-verification). The 2,336-token browser
payload was sent on all 98 calls regardless — fixed dead weight that the
model never benefited from on either task.

## Factor two: call count drives total volume

The per-call prompt is only half the story. The total is per-call prompt
times call count, and the call counts diverge by a factor of nearly two.

| Harness | Spread-plate calls | Durable-job calls |
| --- | ---: | ---: |
| Pi | 24 | 18 |
| OpenCode | 42 | 35 |
| OpenHands | 63 | 35 |

Pi makes fewer calls because it packs more work into each bash invocation.
Pi's first two actions on Spread Plate were:

```bash
pwd && ls -la && git log --oneline -5 2>/dev/null; echo "---"; find . -maxdepth 2 -type f | head -50
git status && echo "---BRANCH---" && git branch -a && echo "---FILES IN LAST COMMIT---" && git show --stat HEAD | head -40
```

OpenHands explored the same repository with nine separate terminal actions,
each its own model call: `pwd && ls -la`, then `git log --oneline -5`, then
`git status; ls -la`, then `find ... -maxdepth 3`, then `find ... -maxdepth 5`,
and so on. Each action sends the full 22-tool prompt again. The exploration
phase alone cost OpenHands nine calls where Pi spent two.

## The two factors compound

Total tokens = calls x per-call prompt. Both factors move in the same
direction, so the total gap is multiplicative, not additive.

On Spread Plate:

| Harness | Calls | Avg prompt | Total prompt | Cached |
| --- | ---: | ---: | ---: | ---: |
| Pi | 24 | 10,596 | 254,314 | 90.3% |
| OpenCode | 42 | 18,788 | 789,088 | 91.8% |
| OpenHands | 63 | 30,249 | 1,905,661 | 94.9% |

Pi: 24 calls x 10.6K = 254K. OpenHands: 63 calls x 30.2K = 1.9M. The 2.6x
call-count ratio times the 2.9x per-call-prompt ratio produces the 7.5x
total-token ratio. The two factors compound.

Caching cannot close the gap. All three harnesses cache roughly 90 to 95
percent of the prompt after the first call, and OpenHands actually has the
highest cache rate (94.9 percent). But 5 percent of a 30K prompt across 63
calls still costs more than 10 percent of a 10K prompt across 24 calls.
Caching reduces the marginal cost of a large prompt; it does not eliminate
the difference in call count.

## The Durable Job Queue case: unused browser schemas on a backend task

Durable Job Queue is the cleanest illustration of the browser-overhead
problem. The task is pure backend — a SQLite-backed job store with states,
retries, crash recovery, and a CLI. There is no frontend, no web server, no
HTML. The verifier is a Python test suite with zero browser dependencies.

OpenHands declared 14 browser tools on all 35 calls and made zero browser
actions. The browser schemas contributed nothing to the solution. From the
provider ledger:

| Harness | Tools | Calls | Avg prompt | Total prompt | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Pi | 4 | 18 | 17,700 | 318,592 | 10/10 pass |
| OpenCode | 10 | 35 | 25,710 | 899,849 | 10/10 pass |
| OpenHands | 22 (14 browser) | 35 | 35,814 | 1,253,493 | 8/10 (2 failed) |

The browser overhead is a fixed constant on every call: the 14 browser tool
schemas total 8,938 characters (2,152 tokens under cl100k_base), and the
browser usage instructions in the system prompt add another 184 tokens. That
is 2,336 tokens per call — the same on call 1 and call 35, since the schemas
and system prompt do not change. Over 35 calls, the unused browser payload
cost roughly 81,800 tokens, or about 6.5 percent of OpenHands' total prompt
spend on this task.

Despite spending more than Pi and OpenCode combined, OpenHands failed two
hidden durability checks. The browser overhead did not buy correctness; it
was fixed dead weight on a task where the browser was irrelevant.

## Why Pi duplicates some commands

One wrinkle: Pi retried 55 percent of its bash commands on Spread Plate
(twelve of twenty-two commands were exact duplicates). The first eight
commands came in identical pairs. This suggests Pi's harness was not
appending tool output to context correctly on the first attempt, or was
re-issuing the same command to confirm.

Despite this, Pi still won on token efficiency because the per-call prompt
and call count were both small enough to absorb the duplication. Pi's
inefficiency (retries) was smaller than OpenHands' inefficiency (large
prompts, many calls).

## Trace and ledger references

Medium-project traces (in [`results/traces/`](traces/)):

- Durable Job Queue:
  [`openhands`](traces/20260824-aws-long-projects-v2-durable-job-queue-openhands.jsonl),
  [`opencode`](traces/20260824-aws-long-projects-v2-durable-job-queue-opencode.jsonl),
  [`pi`](traces/20260824-aws-long-projects-v2-durable-job-queue-pi.jsonl)
- Spread Plate:
  [`openhands`](traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-openhands.jsonl),
  [`opencode`](traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-opencode.jsonl),
  [`pi`](traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-pi.jsonl)

Provider ledger: [`results/provider-ledgers/20260824-aws-long-projects-ledger.jsonl`](provider-ledgers/20260824-aws-long-projects-ledger.jsonl).
The `tool_count` field confirms the per-request tool count (4 for Pi, 10 for
OpenCode, 22 for OpenHands). The `raw_usage.prompt_tokens` and
`raw_usage.prompt_tokens_details.cached_tokens` fields confirm the per-call
prompt sizes and cache rates.
