---
name: log-analyzer
description: Analyze application, JSON, server, and rotated gzip log files to investigate operational, performance, traffic, and reliability signals while producing redacted, evidence-based summaries. Common scenarios include debugging failures, explaining error spikes, finding slow requests, and preparing concise incident notes without modifying log files.
---

# Log Analyzer

## Core Workflow

Analyze logs by deciding what evidence is needed before choosing tools. Stay read-only, sample before filtering, preserve representative raw shape, redact private values, and stop when the evidence supports a useful conclusion.

1. Identify the source: local files, archives, server/runtime logs, cloud logs, observability UI, CI output, or pasted excerpts.
2. Frame the question: time window, deploy/config change, affected service/route/job, expected behavior, and user decision needed.
3. **Estimate volume**: file count, size, approximate line/record count, compression, and observed time span. **Default to narrow first** when any source is over **256 KiB** of raw/uncompressed text, over **2,000 lines/records**, or compressed/rotated with likely expanded size over either limit.
4. Classify records: plain text, JSONL, server access records, runtime logs, multiline traces, compressed/rotated files, or mixed format.
5. Choose available tooling: native log viewer first when present; otherwise use structured queries, editor search, terminal utilities, PowerShell, notebook, or small language snippets.
6. Gather compact evidence: counts, top repeated messages, time range, statuses, endpoints/jobs, hosts/IPs, request IDs, trace IDs, and slow operations.
7. Verify representative examples in redacted, bounded excerpts. Keep facts separate from hypotheses.

Load [references/patterns.md](references/patterns.md) only when concrete command, query, or parser examples are needed.

## Rules

- Do not truncate, rotate, delete, upload, or rewrite logs unless the user explicitly asks for file management.
- Avoid tool lock-in: if a viewer/parser hides important detail, switch to a smaller raw sample.
- Treat counts as signals, not proof; inspect representative records before assigning root cause.
- Do lightweight volume checks before deep scans. Avoid full-log reads when file size, line count, compression, or query cardinality suggests narrowing first.
- Preserve timestamps, source names, line/record positions, correlation IDs, hosts/IPs, methods, endpoints, statuses, and durations when they are not private.
- Redact authorization headers, cookies, tokens, passwords, API keys, session IDs, and query-string credentials before quoting samples.
- Normalize endpoints for counts, but inspect redacted full URLs when parameters or routes matter.
- Stop when additional queries only repeat the same pattern; state what would increase confidence.

## Response

Use this shape:

1. **Summary**: likely incident shape in one or two sentences.
2. **Evidence**: counts, sources, time range, statuses, endpoints/jobs, hosts/IPs, slow operations, and repeated patterns.
3. **Samples**: short redacted excerpts with source and position when available.
4. **Next Checks**: concrete services, deploys, dashboards, traces, queries, or code paths.
5. **Limits**: missing sources, unparseable records, missing rotations, broad windows, or unverified hypotheses.

**Match the user's language by default.**
