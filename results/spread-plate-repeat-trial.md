# Spread-plate OpenHands repeat trial

## Result

We repeated the spread-plate project with native OpenHands and GLM-5.2 on the
same clean AWS benchmark environment. Both the original accepted trial and the
repeat passed the external verifier on the first attempt, with no repair round.
The paths taken to that result differed substantially.

| Measure | Original accepted trial | Repeat trial | Change |
| --- | ---: | ---: | ---: |
| Wall-clock time | 350.737 s | 725.404 s | 2.07x |
| Provider requests | 30 | 45 | 1.50x |
| Requests with complete usage | 30 | 43 | 1.43x |
| Agent tool actions | 29 | 42 | 1.45x |
| Input tokens with reported usage | 914,153 | 1,527,915 | 1.67x |
| Fresh input | 58,857 | 178,027 | 3.02x |
| Cache reads | 855,296 | 1,349,888 | 1.58x |
| Cache-read rate | 93.56% | 88.35% | -5.21 points |
| Output tokens | 21,730 | 23,775 | 1.09x |
| Reported cost | $0.336354 | at least $0.579381 | at least 1.72x |
| External verifier | pass | pass | same |

The repeat cost and token figures are lower bounds. Two provider attempts ended
without usage data, so their tokens and any associated charge are unknown.

## Why the repeat took longer

Two independent effects explain the difference.

### Provider stalls and retries

Two identical requests stalled for 122.052 and 122.695 seconds. Each returned
an error inside an HTTP 200 event stream and no usage object. Their request,
message, system, tool, and stable-prefix fingerprints match exactly, showing
that OpenHands retried the same step rather than advancing the task.

Together these attempts consumed 244.747 seconds:

- 33.7 percent of the repeat's total runtime
- 65.3 percent of the 374.667-second increase over the original trial

Removing those waits leaves approximately 480.7 seconds. The repeat was still
about 37 percent slower than the original trial, so provider latency is not the
whole explanation.

### A longer agent loop

The repeat had 43 successful model calls versus 30 originally. The trace shows
a more elaborate validation and troubleshooting phase: additional browser
checks, test scripts, corrections, file inspections, and repeated final
verification. Both outputs passed the same external contract, so this extra
self-verification did not change the binary quality result.

The conversation grew from 2 messages and 17,829 input tokens on the first
request to 86 messages and 45,874 input tokens on the last. There was no major
prompt-size drop and no condenser event. Calls 31 through 43 alone processed
559,329 input tokens while generating 2,453 output tokens.

The fixed harness context did not drift during the repeat:

- one system-instruction fingerprint across all usage-bearing calls
- one tool-schema fingerprint across all usage-bearing calls
- 42 of 43 successful calls had positive cache reads

Call-count growth explains roughly 65 percent of the added input tokens. The
larger average history carried by each call explains roughly 35 percent. This
was therefore a combination of transient provider behavior and a longer agent
policy trajectory, not a cache failure.

## Measurement correction

The original proxy preserved the two failed response bodies only as hashes and
sizes, but did not classify error events contained inside HTTP 200 streams.
The proxy now recognizes OpenAI- and Anthropic-style streamed provider errors,
marks them as provider errors, and retains only privacy-safe classifications.
It does not save error messages, response content, prompts, or credentials.

This trial demonstrates why repeatability matters. A benchmark should publish
multiple trials, medians, ranges, failure modes, and raw traces rather than
treating one successful run as the characteristic behavior of a harness.

## Evidence

- Raw sanitized run: `raw/reruns/20260824-aws-long-rerun-v1-artifactsbench-spread-plate-openhands.json`
- Content-free provider ledger: `provider-ledgers/20260824-aws-long-rerun-spread-plate-openhands-ledger.jsonl`
- Original accepted comparison: `long-projects.md`

