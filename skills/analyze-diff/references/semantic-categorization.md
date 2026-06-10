# Semantic Categorization

Use this reference when converting file-level diff evidence into a release-oriented feature change list.

## Structural Categories

Group files and hunks by structural change first:

- **Additions**: new files, capabilities, routes, configuration, schemas, tests, or docs.
- **Modifications**: behavior, logic, UI, configuration, tests, or docs changed in existing files.
- **Deletions**: removed files, behavior, config, docs, tests, or public contracts.
- **Renames or moves**: reorganization that may or may not include behavior changes. Check the appendix and representative diffs before calling these no-op refactors.

## Semantic Categories

Translate structural changes into user and engineering meaning:

- **Features**: new user-visible capability, API behavior, workflow, command, setting, or integration.
- **Fixes**: correction to broken behavior, incorrect output, crash, validation issue, or operational failure.
- **Improvements**: non-breaking behavior, UX, reliability, performance, diagnostics, or maintainability improvement.
- **Refactors**: internal structure changes with no intended behavior change.
- **Tests**: coverage additions, changed fixtures, snapshots, test harness changes, or removed tests.
- **Documentation**: user docs, developer docs, inline explanatory content, changelogs, or generated docs.
- **Configuration**: build, environment, dependency, deployment, lint, CI, feature flag, or runtime setting changes.
- **Removals**: deleted behavior, deprecated paths, unsupported options, removed files, or contract narrowing.

## Feature Change List Rules

Default to a user-capability-first list. Each item should name the capability or behavior and include module tags:

```text
- Added project-level export controls [UI, API, config]
- Fixed stale session refresh after account switching [auth, API]
- Improved migration diagnostics for invalid records [CLI, data]
```

Use module tags to preserve engineering locality without making the whole report file-centric. Good tags include `[UI]`, `[API]`, `[CLI]`, `[data]`, `[auth]`, `[config]`, `[deps]`, `[tests]`, `[docs]`, and specific subsystem names visible in the repo.

## Evidence Rules

- Infer user-facing behavior from code only after inspecting the relevant file names, hunks, tests, docs, or commit messages.
- Collapse many files into one capability item when they support the same behavior.
- Split one commit into multiple items when it clearly contains unrelated behaviors.
- Treat tests and docs as supporting evidence, not automatically as release-note items.
- Mention module-only changes under Engineering Notes when they do not produce a user-facing feature, fix, or improvement.
