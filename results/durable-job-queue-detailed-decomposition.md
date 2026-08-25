# Durable Job Queue: detailed token decomposition

This is a per-call decomposition of the token gap on Durable Job Queue, the
pure-backend SQLite task. The task: add a durable SQLite-backed job store
to an existing Python package, with states (queued, running, succeeded,
failed, cancelled), atomic claiming, retries with exponential backoff,
crash recovery, and a cross-process CLI. No frontend, no web server, no
browser interaction. The verifier is a Python test suite with zero browser
dependencies.

## How the three harnesses compared

| Harness | Result | Model calls | Total prompt | Wall clock | Provider cost |
| --- | --- | ---: | ---: | ---: | ---: |
| Pi | 10/10 pass | 18 | 318,592 | 133s | $0.113 |
| OpenCode | 10/10 pass | 35 | 899,849 | 329s | $0.267 |
| OpenHands | 8/10 (2 failed) | 35 | 1,253,493 | 431s | $0.388 |

Pi and OpenCode both passed all 10 durability checks. OpenHands failed 2
hidden durability checks — despite using 4× the tokens, taking 3× as long,
and spending 3.4× the money of Pi. The browser was declared on all 35
OpenHands calls and never used; Pi and OpenCode never declared browser tools
at all.

All token counts below are measured with tiktoken (cl100k_base) against the
trace files, except for `prompt_tokens` and `cached_tokens` which come from
the provider ledger.

## The numbers

| Harness | Model calls | Total prompt | Avg prompt | Cached | Fresh |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pi | 18 | 318,592 | 17,700 | 270,574 (85%) | 48,018 (15%) |
| OpenCode | 35 | 899,849 | 25,710 | 857,728 (95%) | 42,121 (5%) |
| OpenHands | 35 | 1,253,493 | 35,814 | 1,184,512 (94%) | 68,981 (6%) |

Pi and OpenCode are ACP harnesses: their tool schemas live harness-side and
do not appear in the model prompt. OpenHands is a native agent-server
harness: its 22 tool schemas and system prompt are sent in the model prompt
on every call.

## The fixed payload (system prompt + tool schemas)

Measured with tiktoken against the `SystemPromptEvent` in each trace:

| Harness | System prompt | Tool schemas | Total fixed | Per call |
| --- | ---: | ---: | ---: | ---: |
| Pi | 45 tokens | 0 (harness-side) | 45 | 45 |
| OpenCode | 45 tokens | 0 (harness-side) | 45 | 45 |
| OpenHands | 3,528 | 5,793 (22 tools) | 9,321 | 9,321 |

OpenHands pays a 9,321-token fixed overhead on every call. Pi and OpenCode
pay 45. The OpenHands fixed payload breaks down as:

| Component | Tokens per call | Share of fixed |
| --- | ---: | ---: |
| System prompt text | 3,528 | 38% |
| 8 non-browser tool schemas | 3,641 | 39% |
| 14 browser tool schemas | 2,152 | 23% |
| Browser instructions in system prompt | 184 | 2% |
| **Total fixed** | **9,321** | **100%** |

The browser is 2,336 tokens per call — 25% of the fixed payload, but only
6.5% of the average prompt. The system prompt and non-browser tool schemas
together (7,163 tokens) are 3× larger than the browser overhead.

## Prompt growth across calls

The prompt is not constant — it grows as conversation history accumulates.
From the provider ledger:

| Call | Pi | OpenCode | OpenHands |
| ---: | ---: | ---: | ---: |
| 1 | 3,248 | 1,795 | 17,556 |
| 2 | 3,906 | 8,404 | 18,125 |
| 3 | 6,477 | 8,748 | 19,251 |
| 5 | 7,888 | 15,063 | 21,313 |
| 10 | 22,548 | — | — |
| 17 | 25,181 | 27,364 | 37,110 |
| 18 (last for Pi) | 27,089 | 27,863 | 37,609 |
| 35 (last) | — | 36,672 | 49,438 |

OpenHands starts high (17,556 on call 1) because the 9,321-token fixed
payload is already in the first prompt. Pi starts at 3,248 — almost six
times smaller. By the last call, OpenHands has grown to 49,438 while Pi
(topped out at 27,089 on call 18).

The growth rate is similar across harnesses (roughly +1,000–2,000 tokens
per call once steady state is reached), but OpenHands runs for 35 calls
while Pi stops at 18, so its history accumulates further.

## Decomposing the 934,901-token gap

The gap between OpenHands (1,253,493) and Pi (318,592) is 934,901 tokens.
It decomposes into three drivers:

| Driver | Tokens | Share of gap |
| --- | ---: | ---: |
| **Fixed overhead OpenHands pays, Pi doesn't** | 324,660 | 35% |
| — browser schemas (2,336 × 35 calls) | 81,760 | 9% |
| — system prompt + non-browser tools (6,985 × 35) | 242,900 | 26% |
| **Extra calls (35 vs 18, delta = 17)** | 300,127 | 32% |
| — 17 extra calls × Pi's avg variable (17,655) | | |
| **More verbose history growth** | 159,094 | 17% |
| — OpenHands avg variable (26,493) vs Pi (17,655) × 18 calls | | |
| Interaction / residual | 150,820 | 16% |
| **Total gap** | **934,901** | **100%** |

### Driver 1: Fixed overhead (35% of gap)

OpenHands sends a 9,321-token fixed payload on every call; Pi sends 45.
Over 35 calls, that is 326,235 tokens of fixed overhead for OpenHands vs
810 for Pi. The browser is 81,760 of those tokens (9% of the gap); the
system prompt and non-browser tool schemas are 242,900 (26% of the gap).

The non-browser fixed overhead is larger than the browser overhead. The
system prompt alone (3,528 tokens/call × 35 = 123,480) is 1.5× the browser
cost. If the browser were removed but the system prompt and non-browser
tools stayed, OpenHands would still pay 242,900 tokens of fixed overhead
that Pi doesn't.

### Driver 2: Extra calls (32% of gap)

OpenHands makes 35 calls; Pi makes 18. The 17 extra calls each carry the
growing conversation history. At Pi's average variable cost of 17,655
tokens/call, those 17 extra calls account for roughly 300,127 tokens.

This is a harness design choice: Pi packs multiple commands into single
bash invocations (`pwd && ls -la && git log --oneline -5`), while OpenHands
issues one command per model call (`pwd && ls -la`, then `git log`, then
`git status`, each as a separate call).

### Driver 3: More verbose history (17% of gap)

Even on the 18 calls both harnesses made, OpenHands accumulated more
history per call. OpenHands' average variable prompt is 26,493 tokens vs
Pi's 17,655 — a difference of 8,838 tokens/call. Over 18 calls, that is
159,094 tokens.

The verbosity comes from larger observations: OpenHands' observations
average 1,870 tokens each vs Pi's 614. OpenHands' FileEditorAction outputs
include the full file context, and its terminal outputs are verbose.

## The caching effect

94% of OpenHands' prompt tokens are cached. The fixed payload is cached
after call 1, so only 9,321 fresh tokens are paid at full price; the
remaining 316,914 fixed tokens are cached.

| Component | Total tokens | Cached | Fresh |
| --- | ---: | ---: | ---: |
| OpenHands fixed payload | 326,235 | 316,914 (97%) | 9,321 |
| — browser portion | 81,760 | 79,424 (97%) | 2,336 |
| OpenHands variable (history) | 927,258 | 867,598 (94%) | 59,660 |
| **Total** | **1,253,493** | **1,184,512 (94%)** | **68,981** |

Cached tokens are billed at a discount (typically 50% or less of the fresh
rate). The browser overhead is 81,760 prompt tokens, but only 2,336 are
fresh — the rest are cached. In dollar terms, the browser costs even less
than its 6.5% token share suggests.

## What this means

The browser is real dead weight on this task — 2,336 tokens per call, 81,760
total, zero actions — but it is not the primary cost driver. The gap is
driven roughly equally by:

1. **The fixed payload architecture** (35%): OpenHands puts tools and a long
   system prompt in the model prompt; ACP harnesses don't. The browser is
   9% of the gap; the rest is system prompt + non-browser tools.
2. **Call count** (32%): OpenHands makes 2× as many calls, each carrying
   growing history.
3. **History verbosity** (17%): OpenHands' observations are 3× larger,
   inflating the history faster.

Removing the browser would save 81,760 tokens (6.5% of the total) and
zero dollar impact on correctness. But it would not close the gap with Pi,
which is driven more by call count and the non-browser fixed overhead.

## Trace and ledger references

- OpenHands trace: [`results/traces/20260824-aws-long-projects-v2-durable-job-queue-openhands.jsonl`](traces/20260824-aws-long-projects-v2-durable-job-queue-openhands.jsonl)
- Pi trace: [`results/traces/20260824-aws-long-projects-v2-durable-job-queue-pi.jsonl`](traces/20260824-aws-long-projects-v2-durable-job-queue-pi.jsonl)
- OpenCode trace: [`results/traces/20260824-aws-long-projects-v2-durable-job-queue-opencode.jsonl`](traces/20260824-aws-long-projects-v2-durable-job-queue-opencode.jsonl)
- Provider ledger: [`results/provider-ledgers/20260824-aws-long-projects-ledger.jsonl`](provider-ledgers/20260824-aws-long-projects-ledger.jsonl)
