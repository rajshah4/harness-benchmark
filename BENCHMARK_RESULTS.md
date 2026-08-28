# Benchmark Results: Harness × Agent Architecture

## Multi-Agent Orchestration & Validation: Freight Control Tower

A 2×2 experiment used a difficult application task with Sonnet 4.6: build a
durable, multi-tenant freight exception control tower with out-of-order event
replay, idempotency, RBAC, SLA scheduling, a leased outbox, and
Python/API/CLI/browser surfaces.

| Harness | Single agent | Multi-agent completion |
|---------|-------------:|-----------------------:|
| OpenHands native | **6/9** | **6/9** |
| Pi via ACP | **4/9** | **6/9** |

The bounded implementer → validator → orchestrator loop closed Pi's two-point
single-agent gap, but did not exceed OpenHands single-agent quality. The cost
was substantial: multi-agent runs required 87–99 minutes and at least 21–22M
provider tokens, versus 21–26 minutes and 2–3M tokens for the single agents.

Key interpretation: harness choice mattered first; orchestration recovered
specific omissions, but did not raise the observed capability ceiling.

- [Full methodology and results](results/freight-control-tower-sonnet46.md)
- [Presentation slide](results/slides/multi-agent-orchestration-validation-results.pptx)
- [Task, design, and corrected verifier](benchmark/freight-control-tower/)
- [Raw run records](results/raw/generated/)
- [Provider ledgers](results/provider-ledgers/)

---

## CSVQ: Opaque-Oracle CSV Query CLI

All conditions used GLM 5.2 as the LLM.

| Harness | Agent architecture | Score | Cases | Weighted | Elapsed |
|---------|--------------------|------:|------:|---------:|--------:|
| OpenHands native | Single agent | **96.4%** | 47/49 | 81/84 | ~30 min |
| Pi via ACP | Single agent | **96.4%** | 47/49 | 81/84 | ~30 min |
| OpenHands native | Multi-agent system | **100.0%** | 49/49 | 84/84 | ~75 min |
| Pi via ACP | Multi-agent system | **100.0%** | 49/49 | 84/84 | ~12 min |

### Key findings

1. Both harnesses produced identical single-agent results. The two failures
   were the same: `sort-age-reverse-short` and `edge-help`, both exit-code
   mismatches.
2. Both multi-agent systems reached 100%. The validator → orchestrator →
   implementer loop found and repaired edge cases missed by the single agents.
3. Pi's validator created its instrument reliably and its implementer was much
   faster in this run. OpenHands performed more extensive self-verification,
   including 20,000 fuzz cases.
4. The controller enforced a validator wall: only case IDs and mismatch types
   reached the implementer, never oracle outputs.

### Methodology

The task adapted Factory.ai's independent-run methodology:

- An opaque Linux oracle exposed behavior only through probing.
- A 49-case hidden suite assigned weighted importance without committing
  expected outputs.
- An independent validator built a test instrument by probing the oracle.
- The implementer received the specification but not expected outputs.
- The orchestrator relayed constrained failure feedback for repair.
- Final grading compared the candidate with the oracle on the hidden suite.

### Limitations

- One task and one model are not statistically conclusive.
- The OpenHands validator initially needed a more explicit file-creation prompt.
- JSON report parsing used the file API after LLM relay corrupted JSON.
- Timing includes sandbox creation and API latency.

### Files

- [`experiments/csvq/controller/`](experiments/csvq/controller/) — campaign controller and Enterprise API client
- [`experiments/csvq/tasks/csvq/`](experiments/csvq/tasks/csvq/) — task specification, fixtures, oracle, and hidden suite
- [`results/system-openhands-csvq-final.json`](results/system-openhands-csvq-final.json) — OpenHands system result
- [`results/system-pi-csvq-final.json`](results/system-pi-csvq-final.json) — Pi system result
