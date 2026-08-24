# Codex control

Codex was not part of the clean AWS same-model table. The available Codex runs used GPT-5.5 in a local Agent Canvas environment, while the controlled OpenHands, Pi, and OpenCode lanes used GLM-5.2 on AWS.

These numbers are useful as a full-system control, but not as evidence of harness-only efficiency.

| Project | Quality | Time | Tool actions | Fresh prompt tokens | Cache reads | Output tokens | Accounted total | Native reported cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Spread-plate application | Pass | 344.7 s | 13 | 665 | 44,416 | 464 | 45,545 | $0.0172 |
| Durable job queue | 9/9 domain checks; regression check unavailable | 257.7 s | 23 | 2,345 | 42,368 | 958 | 45,671 | $0.0405 |

The durable queue result was recorded as 9/10 because the local verifier environment did not have pytest installed. All nine domain-specific checks passed. The missing regression check is an environment failure, not a demonstrated implementation failure.

The token fields came from Agent Server's native ACP metrics. ChatGPT-authenticated Codex did not expose provider-ledger records through the same GLM-5.2 proxy path, so the semantics and pricing are not guaranteed to match the controlled lanes.

For a valid same-model comparison, rerun Codex and native OpenHands with the same OpenAI model, starting repository, task prompt, environment, timeout, verifier, and provider-boundary usage recorder.

