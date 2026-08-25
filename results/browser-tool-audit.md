# Browser tool audit (medium projects)

This document covers the two medium tasks and answers two questions:

1. Did the medium tasks require the agent to use a browser?
2. Was the browser tool activated (declared and used) on each trace, and why?

## The two medium tasks

### Durable Job Queue

A pure backend task. The agent receives a Python package (`jobboard`) that
runs jobs in memory and must add a durable SQLite-backed execution path.

The spec requires:

- A `SQLiteJobStore` with states (queued, running, succeeded, failed,
  cancelled), atomic claiming, retries with exponential backoff, crash
  recovery, and cancellation
- A `DurableJobRunner` that recovers interrupted jobs and runs until idle
- A cross-process CLI

There is no frontend, no web server, no HTML, no browser interaction. The
verifier (`verify_durable.py`) is a Python test suite with zero browser,
Playwright, or Selenium dependencies.

**Browser requirement: none.**

### Artifactsbench Spread Plate

A static web app task. The agent must build a self-contained `index.html`
that demonstrates a biology lab technique (spread plate) with original
SVG/HTML/canvas artwork, a JavaScript state machine, and DOM markers.

The spec requires:

- Static HTML/CSS/SVG artwork (no remote assets, CDNs, or libraries)
- A `window.spreadPlateLab` object with `getState()`, `perform(action)`,
  `reset()`
- DOM markers like `data-testid="lab-root"`
- The app must run by opening or statically serving `index.html`

The verifier (`verify_spread_plate.py`) does use Playwright/Chromium. It
launches a headless browser, navigates to a static-server URL, and checks
DOM markers and the JS contract. But this is the **verifier**, not the
agent. The agent builds the app by writing files. It does not need a
browser tool to create HTML, write JavaScript, or self-verify. A static
file inspection or `curl` of the HTML source suffices.

**Browser requirement: none.** The verifier uses Playwright, but the agent
does not need a browser tool to build the artifact.

## Trace-by-trace audit (medium projects only)

The table below covers the six medium-project traces. "Browser tools
declared" is the count of browser functions in the `SystemPromptEvent.tools`
list. "Browser actions" is the count of actual `Browser*Action` events in
the trace. "HTTP via terminal" counts ACP bash commands that used `curl`,
`http.server`, or similar to fetch a URL (not a browser tool, but a related
self-verification pattern).

| Trace | Harness | Browser tools declared | Browser actions used | HTTP via terminal |
| --- | --- | ---: | ---: | ---: |
| durable-job-queue-openhands | OpenHands | 14 | 0 | 0 |
| durable-job-queue-opencode | OpenCode | 0 (ACP) | 0 | 0 |
| durable-job-queue-pi | Pi | 0 (ACP) | 0 | 0 |
| spread-plate-openhands | OpenHands | 14 | 1 | 0 |
| spread-plate-opencode | OpenCode | 0 (ACP) | 0 | 3 |
| spread-plate-pi | Pi | 0 (ACP) | 0 | 4 |

Key observations:

- **OpenHands declared 14 browser tools on every call** (38 calls on Durable
  Job Queue, 63 on Spread Plate). This is the default tool configuration.
- **On Durable Job Queue, the browser was never used.** 14 schemas in every
  prompt, zero actions, zero benefit.
- **On Spread Plate, the browser was used once** — a single
  `BrowserNavigateAction` to `http://localhost:8123/index.html` for
  self-verification of DOM markers. The agent passed the verifier without it.
- **OpenCode and Pi never declared browser tools.** They are ACP harnesses
  whose toolset is harness-side (not visible in the trace). They built both
  solutions using `bash`, `edit`, `write`, and `read`. When they needed to
  check the Spread Plate frontend, they used `python3 -m http.server` +
  `curl`.

## The Durable Job Queue case: zero browser actions, 14 browser schemas

Durable Job Queue is the cleanest illustration. The task is pure backend —
a SQLite-backed job store with no frontend. OpenHands declared 14 browser
tools on all 35 calls and made zero browser actions. Each browser schema
costs roughly 840 to 1,335 prompt tokens, so the 14 unused schemas added
roughly 12,000 to 19,000 tokens per call. Over 35 calls, that is roughly
400,000 to 650,000 tokens (about 33 to 52 percent of OpenHands' total
prompt spend) spent on browser schemas that were never invoked.

Despite this overhead, OpenHands failed two hidden durability checks (8/10)
while Pi and OpenCode both passed 10/10 with zero browser tools. The full
breakdown is in
[`medium-project-token-differences.md`](medium-project-token-differences.md).

## Why the browser was activated for OpenHands

The browser was activated because it is part of OpenHands' default tool
configuration. The standard OpenHands toolset is 22 tools: 8 core (terminal,
file_editor, task_tracker, task, finish, think, switch_llm, invoke_skill) plus
14 browser functions. This toolset is declared in the `SystemPromptEvent` on
every call, regardless of the task.

The browser is not task-conditional. OpenHands does not inspect the task
description and decide whether to include browser tools. It declares the same
22-tool set for a pure backend SQLite store and for a static web app. The
result is that 14 browser schemas (roughly 14,000 to 20,000 prompt tokens)
are sent on every call even when the task has no browser interaction.

When the browser was used on Spread Plate, it was used for self-verification:
the agent navigated to `http://localhost:8123/` to check DOM markers and the
behavior contract. This was never required by the task — the verifier runs its
own Playwright. The browser cost OpenHands twice: once as fixed prompt overhead
on every call, and again as the turn spent on a self-verification action that
added nothing the verifier did not already check.
