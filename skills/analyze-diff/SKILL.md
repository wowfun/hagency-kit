---
name: analyze-diff
description: Analyze git diffs, commit ranges, branch comparisons, pasted diffs, and changesets to produce release-oriented summaries, feature change lists, medium/high risk notes, testing gaps, and draft release notes. Use when asked to explain what changed, compare two commits or branches, prepare changelogs or release notes, assess migration/breaking risk, or turn raw diff evidence into product-plus-engineering change summaries.
---

# Analyze Diff

## Overview

Turn raw change evidence into a concise release-facing analysis. For local git repositories, generate a file change table first and use it as an intermediate evidence artifact before reading representative or high-risk diff details.

## Core Workflow

1. Establish the comparison scope:
   - If the user provides `--base <commit> --target <commit>` or two commit ids, treat the range as `base..target`.
   - If the user provides `--base-branch <branch> --target-branch <branch>`, compare `merge-base(base,target)..target`.
   - If no baseline is provided in a git repository, analyze the current worktree against `HEAD`.
   - If the user only pasted diff text or a change list, analyze the pasted evidence and do not generate CSV artifacts.
2. For local git analysis, run `scripts/build_file_change_table.py` before producing conclusions. Use the generated CSV files as the first evidence layer.
3. Read high-risk or representative diff snippets after inspecting the file table. Prioritize public APIs, schemas, migrations, auth/security, data integrity, config, dependency updates, and large changed files.
4. Categorize changes by user capability first, with module tags such as `[API]`, `[UI]`, `[config]`, `[tests]`, or `[docs]`.
5. Surface only medium/high risks, breaking changes, migrations, and meaningful test gaps in the main report.
6. Produce a layered release-oriented answer.

Load references only when needed:

- Use [references/git-diff-patterns.md](references/git-diff-patterns.md) for git evidence commands and script usage.
- Use [references/semantic-categorization.md](references/semantic-categorization.md) for feature list and module tagging rules.
- Use [references/risk-assessment.md](references/risk-assessment.md) when risk, migration, breaking change, or test coverage judgment is needed.

## Evidence Artifact

For git-backed analysis, create the intermediate table in `.local/diff/`:

```sh
python3 skills/analyze-diff/scripts/build_file_change_table.py
```

Commit range:

```sh
python3 skills/analyze-diff/scripts/build_file_change_table.py --base <commit> --target <commit>
```

Branch compare:

```sh
python3 skills/analyze-diff/scripts/build_file_change_table.py --base-branch <branch> --target-branch <branch>
```

Primary artifact: `.local/diff/file-change-table.csv`

```csv
file_path,file_extension,has_add,has_sub,changed_lines
```

Special file appendix: `.local/diff/special-files.csv`

```csv
file_path,file_extension,change_type,old_path,is_binary,reason,raw_additions,raw_deletions,notes
```

The script uses pandas and assumes it is installed. If pandas or Excel writer packages are unavailable, report the failure plainly; do not install packages unless the user asks.

## Output Contract

Use this shape by default:

1. **Summary**: release theme, scale, highest-impact changes, and confidence.
2. **Feature Change List**: user-capability bullets with module tags, grouped into features, fixes, improvements, and removals when useful.
3. **Engineering Notes**: important implementation, config, dependency, schema, migration, or API details.
4. **Risks & Testing**: only medium/high risks, breaking changes, migration needs, and test gaps.
5. **Release Notes Draft**: concise text suitable for a product-plus-engineering audience.
6. **Limits**: missing baseline, unread files, generated/binary files, broad diff size, or assumptions.

Keep the report evidence-based. Mention the CSV artifact paths and any representative diff commands or files used to support conclusions.

## Rules

- Stay read-only unless the user explicitly asks to modify files.
- Do not treat file counts as semantic proof; inspect representative diff content before claiming user-facing behavior.
- Prefer product-readable language for release notes, but preserve engineering caveats for breaking, migration, config, API, data, and security-sensitive changes.
- For large diffs, analyze from the file table first, then sample high-risk or representative files. State when full diff content was not read.
- Match the user's language by default.
