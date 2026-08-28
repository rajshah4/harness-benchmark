# Adding Pi and OpenCode through ACP

Pi and OpenCode were registered with Agent Canvas as custom
[Agent Client Protocol (ACP)](https://agentclientprotocol.com/) agents. Canvas
remained the common launch, workspace, lifecycle, and event boundary, while
each harness retained its native agent loop and tools.

## Integration shape

```text
benchmark runner
  -> Agent Canvas conversation API
    -> custom ACP profile
      -> pi-acp or opencode acp
        -> common provider-ledger proxy
          -> selected OpenHands LLM provider/model
```

The profile creation scripts are:

- [`runner/configure_glm_acp.py`](../runner/configure_glm_acp.py) for Pi and OpenCode with GLM 5.2.
- [`runner/configure_sonnet_acp.py`](../runner/configure_sonnet_acp.py) for Pi and OpenCode with Sonnet 4.5.
- [`runner/configure_sonnet46_acp.py`](../runner/configure_sonnet46_acp.py) for the Pi/Sonnet 4.6 freight experiment.
- [`runner/configure_deepseek_matrix.py`](../runner/configure_deepseek_matrix.py) for the later DeepSeek comparison.

Each script posts a schema-v2 Canvas profile with `agent_kind: "acp"`,
`acp_server: "custom"`, the controlled model ID, startup and prompt timeouts,
and the command used to start the ACP server.

## Pi

Pi is launched with `pi-acp`. `PI_CODING_AGENT_DIR` points at a committed,
experiment-specific configuration directory, for example:

```text
env PI_CODING_AGENT_DIR=runner/configs/pi-sonnet46 pi-acp
```

The directory contains Pi's `models.json`, `settings.json`, and an empty
`auth.json`. The model configuration sends OpenAI-compatible requests to the
local provider-ledger proxy and reads the credential from the injected
`LLM_API_KEY`; no credential is committed.

For Sonnet 4.6, the relevant files are under
[`runner/configs/pi-sonnet46/`](../runner/configs/pi-sonnet46/). The model entry
pins context and output limits plus compatibility flags for Anthropic cache
controls and reasoning effort.

## OpenCode

OpenCode exposes an ACP server through `opencode acp`. Its profile sets
`OPENCODE_CONFIG` to a committed model configuration and supplies the local
ledger endpoint through `ODSC_LLM_BASE_URL`, for example:

```text
env OPENCODE_CONFIG=runner/configs/opencode-glm.json \
  ODSC_LLM_BASE_URL=http://127.0.0.1:4010/v1/opencode \
  opencode acp
```

The files [`runner/configs/opencode-glm.json`](../runner/configs/opencode-glm.json),
[`runner/configs/opencode-sonnet.json`](../runner/configs/opencode-sonnet.json),
and [`runner/configs/opencode-deepseek.json`](../runner/configs/opencode-deepseek.json)
define an OpenAI-compatible `openhands` provider. They take the API key from
the environment and pin the model and context/output limits for that cell.

## How the runner launches them

[`runner/run_suite.py`](../runner/run_suite.py) maps each benchmark label to its
saved Canvas profile, creates a clean workspace, and starts a conversation via
`POST /api/conversations`. The same task prompt, starter tree, confirmation
policy, iteration cap, metadata, and runtime-secret mechanism are used for
native OpenHands and ACP cells.

For controlled current-main runs, `resolve_curated_agent_settings()`
materializes each profile and attaches the same serialized 11-skill Canvas
allow-list to every harness. This compensates for Agent Server 1.42.1
materializing public skills for native OpenHands but none for ACP profiles.
Public skill loading is disabled so the context is explicit and repeatable.

## Usage accounting

ACP-reported usage is retained as diagnostic evidence but is not assumed to be
directly comparable with native OpenHands usage:

- Pi reports session statistics through its ACP response.
- OpenCode's aggregate session counters are also read from its local SQLite
  session database by `opencode_usage()`.
- Cross-harness comparisons use the common provider-boundary ledger produced by
  [`runner/provider_ledger_proxy.py`](../runner/provider_ledger_proxy.py).

The ledger stores provider-returned usage, timing, request structure, and
content-free hashes. It does not store prompts, responses, or credentials. The
calibration and publication gates are documented in
[`runner/MEASUREMENT-PROTOCOL.md`](../runner/MEASUREMENT-PROTOCOL.md).

## Reproduction prerequisites

The Canvas host must have `pi-acp` and `opencode` installed, an OpenHands
provider credential available as `LLM_API_KEY`, and the provider-ledger proxy
listening at the endpoint encoded in the selected harness configuration. Run
the matching configuration script, verify the profiles through Canvas's API,
and execute the calibration gates before publishing token comparisons.
