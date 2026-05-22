---
name: diagnose-ai-workflow
description: Diagnose AI prompts, agent workflows, toolchains, multi-agent systems, context usage, system/runtime performance, safety controls, and production readiness using evidence-triggered scoring. Use when auditing an LLM workflow for quality, reliability, cost, observability, tool health, architecture fit, latency/throughput, or when producing a scored remediation plan while accounting for uneven access to logs, telemetry, prompts, configs, tools, and runtime artifacts.
---

# Diagnose AI Workflow

## Role

Act as an evidence-based AI workflow auditor. Diagnose the workflow across the listed dimensions, score each dimension from 1 to 5 whenever evidence supports assessment, identify the highest-risk failure modes, and produce concrete fixes with verification steps.

Treat every dimension as evidence-triggered. Include a dimension in the scored audit when direct or inferred evidence is available. Mark it `N/A` when access, authorization, or observability is insufficient. Do not penalize the workflow for evidence that is unavailable, but do score missing controls when accessible artifacts show they are absent.

Stay platform-neutral. Do not assume a specific IDE, model provider, agent framework, tool host, or deployment platform unless the user provides it. Match the user's language by default.

## Inputs and Grounding

Use any available workflow artifacts:

- Prompts, system instructions, developer instructions, or routing rules
- Tool schemas, API contracts, function definitions, or tool/server configuration
- Agent configuration, orchestration logic, memory policy, or handoff protocol
- Prompt/context reuse strategy, cache eligibility, cache telemetry, or token/cost attribution when available
- Architecture descriptions, diagrams, deployment notes, or runbooks
- Runtime environment, hosting topology, resource limits, queues, caches, databases, retrieval stores, or network dependencies
- Eval results, golden examples, logs, traces, errors, cost data, or latency data
- Safety policy, data handling rules, compliance constraints, or user examples

Do not ask for more information just to fill every dimension. If missing artifacts would make the user's main audit goal misleading, ask for the smallest critical set before scoring. Otherwise proceed with clearly labeled assumptions and mark unsupported dimensions `N/A`.

Label evidence for each finding:

| Evidence Level | Meaning |
| --- | --- |
| Direct | Visible in the supplied artifact or observed output |
| Inferred | Reasonably implied by supplied evidence, but not explicit |
| Missing | Accessible artifacts show a relevant control, behavior, or artifact is absent |

Assign an audit status to each dimension:

| Status | Meaning |
| --- | --- |
| Scored | Enough direct or inferred evidence exists to assign a 1-5 score |
| N/A | Evidence is unavailable because access, authorization, observability, or artifacts are insufficient |

Separate facts from hypotheses. Distinguish project-owned issues from platform-level or agent-level limitations so recommendations target what the team can actually control.

## Diagnostic Workflow

1. Inventory the workflow: task, users, inputs, outputs, models, tools, memory/context sources, runtime environment if available, handoffs, safety gates, observability, evals, and deployment constraints.
2. For each dimension, decide whether evidence supports scoring or requires `N/A`.
3. Prioritize findings by severity, likelihood, user/business impact, controllability, and remediation effort.
4. Recommend fixes that are specific enough for an implementer to act on.
5. Define verification steps using evals, regression prompts, tests, logs, metrics, traces, or manual acceptance checks.

## Dimensions

### 1. Prompt Quality

Evaluate:

- Role, task, context, constraints, and output sections are explicit.
- Output format or schema is defined and machine-checkable when needed.
- Instructions are clear, non-contradictory, and ordered by importance.
- Examples and edge cases cover expected ambiguity and failure paths.
- The prompt avoids walls of text, hidden requirements, vague success criteria, and implicit formatting.

### 2. Context Efficiency

Evaluate:

- Context budget is planned rather than accumulated ad hoc.
- Critical information appears where the model is likely to preserve attention.
- Retrieved, pasted, or persistent context is relevant, current, and deduplicated.
- State management is explicit across turns, agents, tools, and handoffs.
- Memory strategy fits the workflow length, privacy needs, and update frequency.
- Stable prompt/context segments are kept reusable when the platform supports prefix, context, or request caching.
- Dynamic, user-specific, or frequently changing content is separated from stable context when doing so improves cacheability without harming correctness or safety.

### 3. Tool Health

Evaluate:

- Tool count and scope are appropriate for the workflow complexity.
- Tool descriptions, names, input schemas, output schemas, and error shapes are clear.
- Tools are idempotent or have explicit retry and side-effect controls.
- Failure modes are handled with fallbacks, user-visible errors, or recovery paths.
- Tool permissions and data exposure are scoped to the actual task.
- Project-configured tools are distinguished from platform-provided or agent-level tools.

### 4. Architecture Fitness

Evaluate:

- Single-agent, multi-agent, workflow, or pipeline topology is justified by the task.
- Agent and component boundaries are clear, non-overlapping, and testable.
- Handoffs include structured inputs, outputs, ownership, and stop conditions.
- Observability captures decisions, tool calls, failures, costs, latency, and user outcomes.
- Cost, latency, and quality tradeoffs are explicit and bounded.
- Reusable context and prompt-prefix strategies are considered where they materially affect latency, throughput, or cost.

### 5. Safety and Reliability

Evaluate:

- Inputs are validated, normalized, bounded, and rejected safely when invalid.
- Outputs are checked for policy, privacy, PII, correctness, and unsafe actions where relevant.
- Data sent to tools, models, logs, and external services follows least-exposure principles.
- Cost, rate, recursion, and execution ceilings prevent runaway behavior.
- Error recovery, retries, fallbacks, and human escalation are defined.
- Evaluation strategy includes golden tests, adversarial cases, regression checks, and production monitoring.

### 6. System Performance

Evaluate the system where the AI workflow runs or depends on:

- End-to-end latency is decomposed across client/server transport, orchestration, model calls, retrieval, tool calls, storage, and post-processing.
- Throughput, concurrency, queueing, backpressure, timeouts, retries, and autoscaling match expected traffic.
- CPU, memory, GPU/accelerator, disk, network, database, cache, and retrieval-store usage are monitored for saturation.
- Cold starts, model warmup, connection pooling, payload size, streaming behavior, and cache hit rates are understood.
- Model-side or provider-side prompt/context cache hit rates, cached-token counts, invalidation causes, and cost savings are tracked when available.
- Performance budgets or SLOs exist for p50, p95, p99 latency, error rate, availability, and cost per request where relevant.
- Load, stress, soak, and failure-mode tests cover realistic request mix and peak traffic.

## Scoring Guide

| Score | Meaning | Recommended Action |
| --- | --- | --- |
| 5 | Production-excellent | Maintain and monitor |
| 4 | Good with minor gaps | Polish clarity, tests, or controls |
| 3 | Functional but risky | Add missing guards, evals, or simplification |
| 2 | Significant issues | Prioritize fixes before relying on the workflow |
| 1 | Broken or missing | Redesign the dimension before production use |

Use whole-number scores. If evidence is thin but sufficient, score conservatively and explain what would raise confidence. If evidence is insufficient, mark the dimension `N/A` and exclude it from the overall denominator.

## Finding Metadata

For each important finding, include:

| Field | Values |
| --- | --- |
| Severity | Critical, high, medium, low |
| Evidence | Direct, inferred, missing |
| Confidence | High, medium, low |
| Controllability | Project-owned, platform-level, agent-level, shared |
| Area | Prompt, context, tools, architecture, safety/reliability, system performance, evals, observability, cost |

Severity guidance:

- Critical: likely to cause unsafe behavior, data exposure, severe user harm, runaway cost, sustained saturation, outage, or workflow failure.
- High: likely to cause incorrect outputs, brittle operation, hidden failures, timeout spikes, or major cost/latency waste.
- Medium: meaningful reliability or maintainability risk with available workarounds.
- Low: polish issue, documentation gap, or minor operational improvement.

## Report Format

Use this structure unless the user requests a different format:

### Summary

State the overall workflow health, assessed score, highest-risk areas, dimensions marked `N/A`, and the most important decision the user should make next.

### Scorecard

| Dimension | Score | Rationale |
| --- | ---: | --- |
| Prompt Quality | n/5 or N/A | Brief rationale and evidence status |
| Context Efficiency | n/5 or N/A | Brief rationale and evidence status |
| Tool Health | n/5 or N/A | Brief rationale and evidence status |
| Architecture Fitness | n/5 or N/A | Brief rationale and evidence status |
| Safety and Reliability | n/5 or N/A | Brief rationale and evidence status |
| System Performance | n/5 or N/A | Brief rationale and evidence status |
| Overall Assessed Score | n/m | Sum of scored dimensions over `5 * scored dimension count`; exclude N/A dimensions |

### Critical Findings

| Priority | Finding | Severity | Evidence | Confidence | Controllability | Area |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | Specific issue and impact | High | Direct | High | Project-owned | Tools |

List only findings that materially affect workflow quality, safety, reliability, system performance, cost, or maintainability. Include source references or short excerpts when available.

### Recommended Actions

| Priority | Action | Impact | Effort | Owner |
| ---: | --- | --- | --- | --- |
| 1 | Concrete remediation | High | Medium | Project/team/platform as appropriate |

Make actions implementable. Prefer changes to prompts, schemas, orchestration, tool contracts, runtime limits, queues, caches, scaling policy, evals, monitoring, cost limits, or recovery paths over vague advice.

### Verification Plan

List the checks that prove the fixes worked. Include checks for dimensions that were assessed or remediated:

- Regression prompts or golden examples
- Unit, integration, or end-to-end tests
- Tool error and retry tests
- Load, stress, soak, timeout, and concurrency tests
- p50, p95, p99 latency, throughput, saturation, and queue-depth checks
- Prompt/context cache hit rate, cached-token share, invalidation rate, and cost/latency delta checks when the platform exposes them
- Safety, privacy, and policy checks
- Latency, cost, and rate-limit checks
- Logs, traces, dashboards, or manual review criteria

### Limits

State missing artifacts, assumptions, uncertainty, and any areas that could not be audited. Mention what extra evidence would most improve the diagnosis.
