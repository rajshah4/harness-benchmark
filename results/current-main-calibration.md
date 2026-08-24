# Current-main matrix calibration

Before running the long Incident Operations Center matrix, every harness-model
lane completed the same short variable-rename task (`p09-task-01`) in a fresh
workspace. The external verifier passed in all 12 accepted calibrations.

These are calibration results, not performance conclusions. Their purpose is
to prove that each lane reaches the intended provider route and that the
shared ledger captures provider-reported usage and cache status before a long
run is allowed to start.

## GLM-5.2

| Harness | Pass | Time | Provider calls | Tool calls | Input | Cache read | Output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OpenHands | yes | 24.5s | 8 | 8 | 89,379 | 76,544 | 907 |
| Pi | yes | 32.5s | 11 | 10 | 41,996 | 34,176 | 1,102 |
| OpenCode | yes | 32.6s | 9 | 7 | 76,690 | 66,944 | 1,061 |
| Codex | yes | 23.6s | 7 | 6 | 88,157 | 74,112 | 628 |

## DeepSeek V4 Pro

| Harness | Pass | Time | Provider calls | Tool calls | Input | Cache read | Output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OpenHands | yes | 73.2s | 10 | 9 | 125,391 | 108,544 | 2,665 |
| Pi | yes | 51.2s | 7 | 7 | 30,023 | 14,848 | 1,465 |
| OpenCode | yes | 46.7s | 9 | 8 | 84,885 | 69,632 | 1,608 |
| Codex | yes | 48.7s | 10 | 8 | 133,542 | 114,688 | 1,328 |

The first OpenCode DeepSeek calibration used a misplaced configuration file
and bypassed the ledger. It was rejected even though the task passed. The
listed result is the corrected rerun, with nine of nine provider calls
captured.

## Claude Sonnet 4.5

| Harness | Pass | Time | Provider calls | Tool calls | Input | Cache read | Cache write | Output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OpenHands | yes | 110.1s | 16 | 16 | 284,844 | 261,488 | 23,152 | 4,423 |
| Pi | yes | 102.2s | 22 | 22 | 185,233 | 161,921 | 12,824 | 3,180 |
| OpenCode | yes | 75.9s | 15 | 15 | 183,359 | 165,331 | 17,946 | 2,057 |
| Codex | yes | 77.3s | 19 | 18 | 283,504 | 0 | 0 | 2,423 |

Codex's Sonnet Responses requests explicitly returned a cache field with zero
tokens on every response. This is not missing telemetry. It means the Codex
Sonnet lane did not receive provider cache credit under this route and request
format, so its fresh-token cost is not directly comparable to the highly
cached Sonnet lanes without retaining that caveat.

## Evidence gate

For every accepted row:

- provider usage was present for every captured provider response;
- cache status was explicitly present for every response, including reported
  zero values;
- the external verifier passed on the first attempt;
- no repair feedback was supplied; and
- Agent Canvas stored the same 11 pinned default skills.

The sanitized provider ledger is generated as
`rerun-evidence/20260824-current-main-matrix-ledger.jsonl` on the benchmark
host and is copied into `results/provider-ledgers/` for publication after the
long matrix completes.
