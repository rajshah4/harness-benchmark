# Incident Operations Center Long-Project Benchmark

## 1. Introduction

### 1.1 Problem Statement

The current eight-task suite measures short coding loops. The spread-plate lab and durable queue cover broader work, but agents still finish them in roughly five to sixteen minutes. None of these tasks tests whether a harness can understand an existing full-stack application, coordinate changes across several layers, and preserve its plan over a 30-to-60-minute session.

### 1.2 Proposed Solution

Start each harness with the same small incident-tracking application. Ask it to add durable incident operations across SQLite storage, an HTTP API, background escalation, an audit history, and a responsive browser interface.

The task combines objective behavior with visible design choices. Hidden checks verify persistence, concurrency, recovery, API contracts, and browser behavior. Screenshots and traces show how the harnesses organize and execute the work.

The first benchmark is uninterrupted. A later recovery variant stops each agent at the same elapsed-time checkpoint and resumes the same conversation. Keeping these variants separate prevents restart behavior from obscuring the basic efficiency comparison.

## 2. User Experience

An operator can ingest alerts, review active incidents, assign an owner, acknowledge work, resolve an incident, and inspect the complete audit timeline. Filters make it easy to find urgent or unowned incidents. The interface remains usable on a phone-sized viewport.

When two alerts share a fingerprint inside the deduplication window, the application updates one incident instead of opening two. A background worker escalates overdue incidents exactly once, even when multiple worker processes run at the same time. Restarting the application preserves incidents, audit events, assignments, and escalation state.

## 3. Benchmark Shape

The benchmark starts from an existing Python application with an in-memory incident service, a small standard-library HTTP server, a static interface, and regression tests. The harness must preserve the original behavior while adding the long-project requirements.

The repository contains public tests for the starter behavior. The instructor owns the full verifier. The prompt tells the agent what behavior is required but does not reveal the hidden test implementation.

The same GLM-5.2 model runs through OpenHands, Pi, and OpenCode. Each harness receives an isolated copy of the same committed starter tree. The provider ledger records every model request. Laminar records the trace for analysis.

## 4. Technical Design

### 4.1 Durable Incident Model

An incident has an ID, fingerprint, title, severity, status, optional owner, timestamps, SLA deadline, escalation level, and integer version. Valid statuses are `open`, `acknowledged`, and `resolved`. Valid severities are `P1`, `P2`, `P3`, and `P4`.

SQLite stores incidents and append-only audit events. Separate store instances opened on the same database must observe the same state. The schema initializes automatically and supports safe concurrent access from separate processes.

### 4.2 Alert Deduplication

Ingesting an alert creates an incident unless an unresolved incident with the same fingerprint was updated inside the deduplication window. A duplicate increments the incident's alert count, updates its timestamp, and appends an audit event.

Concurrent ingestion of the same fingerprint must still produce one active incident. The implementation must enforce this rule in storage rather than relying on one process-local lock.

### 4.3 State Changes and Audit History

Assignment, acknowledgement, resolution, duplicate ingestion, and escalation append audit events in the same transaction as the incident change. Events remain ordered and survive a restart.

Updates accept an expected version. A stale version returns a conflict instead of overwriting newer work. Repeating the same idempotency key must return the original result without adding a second event.

### 4.4 Escalation Worker

A worker claims overdue unresolved incidents and increments their escalation level. Multiple workers must not escalate the same level twice. Resolved incidents must never be escalated.

The worker accepts an injectable clock and exposes `run_once` and `run_until_idle`. It must recover a claim left behind by an interrupted worker after a documented lease expires.

### 4.5 HTTP API

The existing server gains JSON endpoints to ingest alerts, list and filter incidents, fetch one incident with its timeline, update ownership or status, and run one escalation cycle. Invalid JSON and invalid state changes return useful 4xx responses. Version conflicts return HTTP 409.

### 4.6 Browser Interface

The page shows incident counts, filters, a sortable incident list, a detail panel, owner and status controls, and an audit timeline. Visible feedback explains conflicts and invalid actions. The interface must work at 390 by 844 pixels without horizontal page overflow.

The browser contract uses stable `data-testid` markers and a small `window.incidentOps` test API. The contract verifies behavior without prescribing the visual design.

### 4.7 Export and Import

The CLI exports incidents and audit events as newline-delimited JSON and imports that format into a new database. Export followed by import must preserve incident state and event order without duplicating records when repeated.

### 4.8 Recovery Variant

The uninterrupted run establishes the baseline. The recovery run stops each conversation after fifteen minutes or after a shared observable checkpoint, whichever comes first. The runner then resumes the same conversation with a neutral instruction to continue and records repeated work, lost state, completion time, and final correctness.

## 5. Implementation Plan

Every milestone must keep existing tests passing. The final verifier runs unit checks, cross-process concurrency checks, CLI checks, an HTTP workflow, and Playwright browser checks.

### 5.1 Starter Application

Create the starter package, in-memory service, HTTP server, static interface, CLI shell, and public regression tests. Commit this as the shared baseline tree.

### 5.2 Public Task Contract

Write the participant-facing task with the required data model, API routes, worker behavior, UI markers, and CLI examples. Keep implementation choices open where they do not affect observable behavior.

### 5.3 Instructor Verifier

Add hidden checks for persistence, concurrent deduplication, optimistic concurrency, idempotency, transactional audit events, escalation claims, lease recovery, HTTP errors, export and import, and responsive browser behavior.

### 5.4 Calibration Run

Run one harness against the task to find ambiguous requirements and verifier defects. Fix the task or verifier, reset the workspace, and discard all code produced from invalid feedback.

### 5.5 Controlled Comparison

Run OpenHands, Pi, and OpenCode with GLM-5.2 on clean AWS. Use zero repair rounds for the first-attempt comparison. Record correctness, elapsed time, model calls, context per call, input and output tokens, cache reads, provider cost, tool actions, files changed, and verifier results.

### 5.6 Recovery Comparison

Repeat the benchmark with the shared interruption policy. Report recovery behavior separately from the uninterrupted leaderboard.
