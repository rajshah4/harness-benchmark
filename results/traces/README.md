# Agent traces

This directory contains the complete Agent Canvas event streams for the nine accepted long-project runs:

- three harnesses on the spread-plate application
- three harnesses on the durable job queue
- three harnesses on the full-stack incident project

Each JSONL line is one ordered Canvas event. Events include the task, model messages, tool requests, tool results, system instructions, timestamps, and final response. The provider usage ledger remains separate because it is the token and cost authority.

The traces exclude Canvas `meta.json`, `base_state.json`, lock files, and rejected calibration or invalid-attempt runs. Canvas metadata contains deployment configuration and a large skill catalog that is not part of the executed event stream.

## Redaction

The source archive already represented configured secret values as `[REDACTED]`. The export adds a second defensive pass that removes:

- values under credential-bearing JSON keys
- common OpenAI, GitHub, AWS, Google, Slack, and JWT token formats
- bearer authorization values
- values assigned to common secret environment variables
- machine-specific workspace paths

The export script is [`scripts/export_traces.py`](../../scripts/export_traces.py). The manifest records the run, task, harness, event count, and SHA-256 digest for every exported stream.

