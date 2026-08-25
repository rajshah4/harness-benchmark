# Spread Plate: detailed token decomposition

This is a per-call decomposition of the token gap on Artifactsbench Spread
Plate, the static web app task. The task: build a self-contained `index.html`
that demonstrates a biology lab technique (spread plate) with original
SVG/HTML/canvas artwork, a JavaScript state machine, and DOM markers. The
verifier uses Playwright to check the result, but the agent does not need a
browser tool to write HTML and JavaScript.

All three harnesses passed this task. The differentiator is cost, not
correctness.

## How the three harnesses compared

| Harness | Result | Model calls | Total prompt | Wall clock | Provider cost |
| --- | --- | ---: | ---: | ---: | ---: |
| Pi | pass | 24 | 254,314 | 249s | $0.121 |
| OpenCode | pass | 42 | 789,088 | 409s | $0.357 |
| OpenHands | pass | 63 | 1,905,661 | 351s | $0.612 |

All three passed. OpenHands used 7.5× the tokens and spent 5× the money of
Pi. OpenHands made one browser action (a single navigate to
`http://localhost:8123/index.html` for self-verification); Pi and OpenCode
made zero. Notably, OpenHands was faster in wall clock than OpenCode (351s
vs 409s) despite making 21 more calls — its heavy caching makes individual
calls cheap.

All token counts below are measured with tiktoken (cl100k_base) against the
trace files, except for `prompt_tokens` and `cached_tokens` which come from
the provider ledger.

## The fixed payload

Identical to Durable Job Queue — the fixed payload is a property of the
harness, not the task:

| Harness | System prompt | Tool schemas | Total fixed | Per call |
| --- | ---: | ---: | ---: | ---: |
| Pi | 45 tokens | 0 (harness-side) | 45 | 45 |
| OpenCode | 45 tokens | 0 (harness-side) | 45 | 45 |
| OpenHands | 3,528 | 5,793 (22 tools) | 9,321 | 9,321 |

The OpenHands fixed payload:

| Component | Tokens per call | Share of fixed |
| --- | ---: | ---: |
| System prompt text | 3,528 | 38% |
| 8 non-browser tool schemas | 3,641 | 39% |
| 14 browser tool schemas | 2,152 | 23% |
| Browser instructions in system prompt | 184 | 2% |
| **Total fixed** | **9,321** | **100%** |

## Prompt growth across calls

| Call | Pi | OpenCode | OpenHands |
| ---: | ---: | ---: | ---: |
| 1 | 3,233 | 1,783 | 17,549 |
| 2 | 3,473 | 8,401 | 17,811 |
| 3 | 3,606 | 8,596 | 18,034 |
| 5 | 3,956 | 9,297 | 28,089 |
| 12 | 3,686 | — | — |
| 13 | 6,348 | — | — |
| 14 | 15,355 | — | — |
| 24 (last for Pi) | 20,576 | — | — |
| 31 | — | 37,571 | — |
| 42 (last for OpenCode) | — | 24,671 | — |
| 61 | — | — | 40,368 |
| 63 (last) | — | — | 41,101 |

Pi's prompt stays flat at 3–4K for the first 12 calls, then jumps to 15K+
at call 14 — likely when it starts writing the HTML file and the file
content enters the conversation. OpenHands starts at 17,549 (the fixed
payload alone is 9,321) and grows steadily. By the last call, OpenHands is
at 41,101 while Pi peaked at 20,576.

## Decomposing the 1,651,347-token gap

The gap between OpenHands (1,905,661) and Pi (254,314) is 1,651,347 tokens.

| Driver | Tokens | Share of gap |
| --- | ---: | ---: |
| **Fixed overhead OpenHands pays, Pi doesn't** | 584,388 | 35% |
| — browser schemas (2,336 × 63 calls) | 147,168 | 9% |
| — system prompt + non-browser tools (6,985 × 63) | 437,220 | 26% |
| **Extra calls (63 vs 24, delta = 39)** | 411,505 | 25% |
| — 39 extra calls × Pi's avg variable (10,551) | | |
| **More verbose history growth** | 249,028 | 15% |
| — OpenHands avg variable (20,928) vs Pi (10,551) × 24 calls | | |
| Interaction / residual | 406,426 | 25% |
| **Total gap** | **1,651,347** | **100%** |

The residual is larger here (25%) than on Durable Job Queue (16%) because
the call-count ratio is bigger (2.6× vs 1.9×), so the multiplicative
interaction between "more calls" and "higher per-call variable" is stronger.

### Driver 1: Fixed overhead (35% of gap)

Same as Durable Job Queue: OpenHands sends 9,321 tokens of fixed payload per
call; Pi sends 45. Over 63 calls, that is 587,223 tokens of fixed overhead.
The browser is 147,168 of those (9% of the gap); the system prompt and
non-browser tools are 437,220 (26%). The non-browser fixed overhead is 3×
larger than the browser overhead.

### Driver 2: Extra calls (25% of gap)

OpenHands made 63 calls; Pi made 24. The 39 extra calls each carry growing
conversation history. At Pi's average variable cost of 10,551 tokens/call,
those 39 extra calls account for roughly 411,505 tokens.

The call-count ratio is larger on Spread Plate (2.6×) than on Durable Job
Queue (1.9×). This is the biggest difference between the two tasks:
Spread Plate is a larger task that rewards Pi's packed-bash-call strategy
even more.

### Driver 3: More verbose history (15% of gap)

OpenHands' average variable prompt is 20,928 tokens vs Pi's 10,551 — a
difference of 10,377 tokens/call. Over 24 calls, that is 249,028 tokens.

## The caching effect

| Harness | Total prompt | Cached | Fresh | Cache rate |
| --- | ---: | ---: | ---: | ---: |
| Pi | 254,314 | 229,549 | 24,765 | 90% |
| OpenCode | 789,088 | 724,160 | 64,928 | 92% |
| OpenHands | 1,905,661 | 1,808,256 | 97,405 | 95% |

OpenHands has the highest cache rate (95%) because its large fixed payload
(9,321 tokens) is cached after call 1, and with 63 calls the history caching
amortizes well. The browser overhead is 147,168 tokens, but only 2,336 are
fresh — the rest are cached at a discount.

This is why OpenHands was faster in wall clock than OpenCode despite making
more calls: 95% of its tokens are cached, so individual calls are cheap.

## The browser on Spread Plate

OpenHands made exactly one browser action on Spread Plate: a
`BrowserNavigateAction` to `http://localhost:8123/index.html` with the
thought "Let me verify the DOM markers and behavior contract with a headless
script using the browser." The agent passed the verifier without it — the
verifier runs its own Playwright.

The browser cost 147,168 tokens (2,336 × 63 calls) and contributed one
action that was not needed. But this is 7.7% of the total, not the main
driver of the gap.

## Pi's duplicate commands

Pi duplicated 55% of its bash commands on Spread Plate — 12 of 22 commands
were exact duplicates, with the first 8 coming in identical pairs. This
suggests Pi's harness was not appending tool output to context correctly on
the first attempt, or was re-issuing the same command to confirm.

Despite this, Pi still won by a wide margin because its per-call prompt
(10,596 avg) and call count (24) were both small enough to absorb the
retries. Pi's inefficiency (duplicates) was smaller than OpenHands'
inefficiency (large fixed payload, many calls).

## What this means

The Spread Plate results mirror Durable Job Queue:

1. **Fixed payload architecture (35%)** — the largest single driver.
   OpenHands' 9,321-token fixed overhead × 63 calls = 587K tokens that Pi
   doesn't pay. The browser is 9% of the gap; the system prompt and
   non-browser tools are 26%.
2. **Call count (25%)** — OpenHands made 2.6× as many calls. This is a
   harness design choice: Pi packs commands; OpenHands uses one call per
   command.
3. **History verbosity (15%)** — OpenHands' observations are larger,
   inflating the history faster.
4. **Interaction (25%)** — the multiplicative effect of more calls × higher
   per-call variable.

All three harnesses passed. The gap is purely a cost and efficiency
difference, not a correctness difference.

## Trace and ledger references

- OpenHands trace: [`results/traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-openhands.jsonl`](traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-openhands.jsonl)
- Pi trace: [`results/traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-pi.jsonl`](traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-pi.jsonl)
- OpenCode trace: [`results/traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-opencode.jsonl`](traces/20260824-aws-long-projects-v2-artifactsbench-spread-plate-opencode.jsonl)
- Provider ledger: [`results/provider-ledgers/20260824-aws-long-projects-ledger.jsonl`](provider-ledgers/20260824-aws-long-projects-ledger.jsonl)
