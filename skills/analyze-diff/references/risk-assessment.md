# Risk Assessment

Use this reference when deciding which changes need risk, migration, or testing notes in the release-oriented report.

## Risk Indicators

Flag medium or high risk when changes touch:

- **Breaking contracts**: public APIs, CLI flags, function signatures, return types, schemas, protocol fields, serialized data, request/response shapes, or documented behavior.
- **Security-sensitive paths**: authentication, authorization, session handling, credentials, secrets, cryptography, input validation, sanitization, network boundaries, or permission checks.
- **Data integrity**: writes, migrations, persistence, serialization, transactions, idempotency, deduplication, validation constraints, or destructive operations.
- **External services and packages**: third-party services, API clients, SDK versions, package upgrades, infrastructure services, queues, caches, databases, or providers.
- **Performance-critical paths**: hot loops, query behavior, caching, concurrency, memory allocation, file/network IO, startup paths, or background jobs.
- **Configuration and rollout**: environment variables, feature flags, CI/deploy behavior, infrastructure settings, build targets, or backwards-compatibility switches.

## Risk Levels

- **Low**: docs, tests, internal-only refactors, isolated code cleanup, or clearly additive changes with no production behavior impact.
- **Medium**: new features, config/dependency changes, non-breaking API extensions, broad refactors, changes with partial tests, or behavior changes with limited blast radius.
- **High**: breaking changes, migrations, security/auth changes, data writes, dependency upgrades with compatibility uncertainty, critical path changes, or broad changes without evidence of tests.

Only include medium and high risks in the default report. Do not fill space with low-risk boilerplate.

## Testing Gap Heuristics

Call out a test gap when:

- Production code changes without nearby tests, fixtures, docs, or verification notes.
- A migration, schema, API, auth, data, or dependency change lacks integration or regression coverage.
- Tests were deleted or snapshots changed without clear replacement evidence.
- The diff changes configuration or deployment behavior but no validation path is visible.
- Binary/generated output changed and the source of truth is unclear.

## Risk Output Shape

Use concise, actionable bullets:

```text
- High: API response shape changed in `orders` endpoints [API, data].
  Mitigation: run compatibility tests for existing clients and document migration notes.
- Medium: cache invalidation logic changed without visible regression tests [backend].
  Mitigation: add coverage for stale reads and refresh behavior.
```

Separate evidence from inference. If the risk is inferred from file paths or partial hunks, say so.
