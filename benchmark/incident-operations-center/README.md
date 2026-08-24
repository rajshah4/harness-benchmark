# Incident Operations Center Benchmark

This is the workshop's genuinely long coding-agent benchmark. It starts from a working in-memory incident application and asks the harness to add durable storage, concurrency, background escalation, a full HTTP and CLI workflow, and a responsive browser interface.

The benchmark is designed to take 30 to 60 minutes. It complements the eight short tasks and the two medium projects.

## Files

- `DESIGN.md` explains why this task exists and how it will be measured.
- `task.md` is the prompt given to every harness.
- `starter/` is the committed baseline copied into each isolated workspace.
- `verify_incident_ops.py` is the instructor-owned verifier. It is never copied into the agent workspace.

## Validate the starter

The starter's public regression tests must pass:

```bash
cd starter
python -m pytest -q
```

The hidden verifier should report only the regression check as passing before the task is implemented:

```bash
python verify_incident_ops.py starter
```

## Run It Manually

A manual run is enough to study one harness or compare two harnesses. It does not require the full AWS runner.

1. Configure each harness with the same model, endpoint, and model settings.
2. Copy `starter/` into a separate clean workspace for each harness.
3. Start a new conversation with no prior harness memory.
4. Send the exact contents of `task.md`.
5. Use the same permissions, network policy, timeout, and repair policy for every run.
6. Budget 90 minutes, although the measured runs finished in 18 to 27 minutes.
7. Save the conversation history and provider usage before changing the workspace.
8. Run the verifier from outside the agent workspace:

```bash
python verify_incident_ops.py /absolute/path/to/harness-workspace
```

Record these fields for every run:

| Measurement | Why keep it |
|---|---|
| Verifier sections passed | Measures required behavior independently of the agent |
| Wall-clock time | Captures the complete user wait |
| Model calls | Measures how long the agent loop ran |
| Input and output tokens | Measures provider work |
| Cache-read tokens | Separates reused context from new input |
| Context per model call | Separates large requests from many requests |
| Tool actions | Shows how the model calls became environment work |
| Provider errors and retries | Explains time that the harness did not control |

Provider responses should supply the token and cache fields. Do not estimate them from text length. If the provider does not report a field, mark it unavailable.

## Controlled run

On the clean AWS benchmark host:

```bash
ODSC_RUN_PREFIX=20260824-aws-incident-v1 \
  ../harness-suite/run_aws_incident_project.sh
```

The first comparison uses zero repair rounds. The provider ledger supplies token, cache, call, and cost measurements. Laminar supplies trace inspection. Run IDs, task and verifier hashes, isolated baseline trees, timing, diffs, and verifier output are stored with each result.

The controlled path requires:

- A configured Agent Canvas instance with OpenHands, Pi, and OpenCode profiles
- One shared model endpoint for all three profiles
- The provider ledger proxy from `../harness-suite/provider_ledger_proxy.py`
- A verifier environment with the browser dependencies installed
- Explicit approval before exporting prompt and tool content to Laminar

Create the verifier environment before starting a long run:

```bash
python3 -m venv .venv-verifier
.venv-verifier/bin/pip install pytest playwright
.venv-verifier/bin/playwright install chromium
```

Pass `.venv-verifier/bin/python` to `run_suite.py --verifier-python`, or use
`runner/run_current_main_long_matrix.py`, which selects that path by default.
Treat a missing Playwright or pytest import as a verifier setup failure, not a
solution failure.

Run the ledger calibration gates in `../harness-suite/MEASUREMENT-PROTOCOL.md` before publishing token or cost comparisons.

## Recovery variant

Do not mix interruption recovery into the first leaderboard. After the uninterrupted comparison is valid, repeat the task with one shared stop-and-resume policy and report it as a separate experiment.
