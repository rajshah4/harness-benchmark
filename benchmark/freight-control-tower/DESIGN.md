# Freight Exception Control Tower Benchmark

## 1. Introduction

### 1.1 Problem Statement

The previous incident-operations task was saturated: OpenHands single-agent and the completion system both passed every capability. A harder workshop exercise must require sustained integration across persistence, temporal ordering, authorization, concurrency, background work, and a usable interface without turning into a synthetic unit-test puzzle.

### 1.2 Proposed Solution

The benchmark asks an agent to evolve a small in-memory shipment tracker into a durable, multi-tenant freight exception control tower. Carrier events may arrive late or more than once. Operators acknowledge and resolve exceptions under optimistic concurrency. SLA rules enqueue durable notifications through a leased outbox. API, CLI, export, and browser surfaces must enforce the same tenant and role boundaries.

The task remains a build-an-app exercise. The public contract describes user-visible behavior and invariants; an instructor-owned verifier scores eight capabilities through public interfaces. It does not require a specific schema or internal architecture.

## 2. User Interface

An operator opens a responsive dashboard, selects a tenant, filters delayed shipments, and sees the current exception queue. They can acknowledge an exception with its displayed version and receive visible success or conflict feedback. An administrator can create tenants and API tokens, replay dead-lettered deliveries, and export or restore a tenant snapshot from the CLI.

## 3. Other Context

The starter uses only the Python standard library at runtime. Candidates may add dependencies, but timed verification cannot depend on an external database, broker, or network service. SQLite is the expected durable substrate.

## 4. Technical Design

### 4.1 Capability model

The independent verifier awards one point for each of eight capabilities: durable restart, event replay, idempotent ingestion, exception concurrency, tenant/RBAC isolation, SLA scheduling, leased outbox recovery, and complete HTTP/CLI/browser operations.

### 4.2 Information boundaries

All three conditions receive the identical `task.md` and starter tree. The implementer never receives verifier source or exact cases. The controller passes only capability-level outcomes to the validator. Validator and orchestrator use separate workspaces and cannot edit the candidate.

### 4.3 Completion system

The system condition uses one persistent implementer conversation plus fresh validator and orchestrator conversations per round. Each role has one responsibility and a machine-readable output contract. The orchestrator alone selects `STOP` or `CONTINUE`; at most two repair rounds are available.

### 4.4 Measurement

Provider receipts are collected at the OpenHands provider boundary and labeled by run, task, condition, role, and phase. Publication requires the exact Sonnet 4.6 request model and calibrated request parameters, non-duplicated response IDs, and valid usage on every successful call. Non-2xx attempts remain separate reliability incidents.

## 5. Implementation Plan

Acceptance criteria: the starter's public tests pass, the starter fails most instructor capabilities, verifier results are deterministic, all runner tests pass, and the AWS run uses the established 1.15.0 Canvas / 1.42.1 agent-server environment.

### 5.1 Starter and public contract

Add `benchmark/freight-control-tower/starter/`, `task.md`, and `README.md`. The starter demonstrates shipment creation and a basic dashboard but intentionally lacks durability and production workflows.

### 5.2 Independent verifier

Add `verify_freight_control_tower.py`. Exercise public Python, HTTP, CLI, restart, concurrency, and browser behavior while reporting only named capabilities to the completion controller.

### 5.3 Runner integration

Teach `run_suite.py` and `run_completion_experiment.py` to select this task without changing the existing incident evidence path. Record task and verifier hashes.

### 5.4 AWS experiment

Run OpenHands single, Pi single, and OpenHands completion system sequentially with Sonnet 4.6, high reasoning, the same 11 Vasco-default skills, fresh workspaces, and a fresh provider ledger.
