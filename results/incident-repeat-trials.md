# Incident project repeat trials

## What we repeated

We reran the full Incident Operations Center project on the same clean AWS
benchmark machine. The second native OpenHands trial used GLM-5.2, the same
model as the original three-harness comparison. We then held the OpenHands
harness fixed and changed the model to Claude Sonnet 4.5 through the OpenHands
provider.

Both reruns used the corrected task and verifier, fresh workspaces, no repair
round, the provider-boundary ledger, and an external eight-part verifier.

## Results

| Harness and model | Quality | Time | Calls | Tool actions | Input | Fresh input | Cache rate | Output | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OpenCode + GLM, original | 8/8 | 17m 40s | 76 | not comparable | 2.75M | 201K | 92.7% | 39.9K | $0.768 |
| Pi + GLM, original | 7/8 | 20m 21s | 69 | not comparable | 3.16M | 80K | 97.5% | 58.4K | $1.054 |
| OpenHands + GLM, original | behavior passed | 26m 42s | 95 | not comparable | 6.76M | 1.87M | 72.3% | 50.0K | $2.614 |
| OpenHands + GLM, repeat | 8/8 | 17m 23s | 129 | 131 | 10.20M | 1.27M | 87.6% | 53.0K | $1.522 |
| OpenHands + Sonnet, repeat | 7/8 | 12m 24s | 66 | 65 | 4.26M | 98K | 97.7% | 42.5K | unavailable |

Agent tool-action counts are omitted for the original incident cells because
the earlier traces did not use the same finalized counting method.

## Did the second GLM run close the gap?

It closed the time gap, but not the resource gap.

The repeated OpenHands GLM run finished slightly faster than OpenCode and about
15 percent faster than Pi. The provider was healthier this time: all 129 calls
returned usage, median provider latency was 4.1 seconds, and there were no
failed requests.

OpenHands still used 1.7 times as many calls and 3.7 times as much input as
OpenCode. Compared with Pi, it used 1.9 times as many calls and 3.2 times as
much input. Its cost remained about twice OpenCode's and 44 percent above Pi's.

Compared with the first OpenHands trial, the repeat made more calls and sent
more total context, but it read much more of that context from cache. Fresh
input fell from 1.87 million to 1.27 million, and the reported cost fell from
$2.61 to $1.52. Better provider behavior and cache use made the run faster and
cheaper without making the agent loop lighter.

## What changed with Sonnet

Holding OpenHands fixed and switching to Sonnet cut the loop roughly in half:
66 calls instead of 129, 65 tool actions instead of 131, and 4.26 million input
tokens instead of 10.20 million. Sonnet finished in 12 minutes 24 seconds.

That closes the time and call-count gaps with Pi and OpenCode. It does not close
the input-token gap. Sonnet still sent 35 percent more input than Pi and 55
percent more than OpenCode.

Sonnet also missed one external check. The backend, concurrency, persistence,
HTTP, CLI, and export/import checks passed. The browser check failed because
the generated JavaScript declared `acknowledgeBtn` and `resolveBtn` twice with
`const` inside the same function. The syntax error stopped the page script, so
the summary never rendered. This was a real implementation defect, not a
verifier error.

The OpenHands provider did not return a Sonnet cost field. We report cost as
unavailable rather than zero.

## What the repeats tell us

The broad pattern held. OpenHands with the same GLM model still carried much
more context and made more calls than the ACP harnesses. A healthier provider
and better cache rate improved time and cost, but did not reduce the underlying
loop size.

The Sonnet result shows that the model can change the path substantially even
when the harness stays fixed. Sonnet was much more concise, but the shorter
path missed a simple frontend error. Time, calls, tokens, cache use, and an
external quality check all tell different parts of the story.

## Evidence

- `raw/reruns/20260824-aws-incident-rerun-v2-openhands.json`
- `raw/reruns/20260824-aws-incident-rerun-v1-openhands-sonnet.json`
- `provider-ledgers/20260824-aws-incident-rerun-openhands-ledger.jsonl`
- `provider-ledgers/20260824-aws-incident-rerun-sonnet-ledger.jsonl`
- Original comparison: `incident-project.md`

