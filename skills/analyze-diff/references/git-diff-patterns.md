# Git Diff Patterns

Use this reference for git-backed evidence gathering and the file change table script.

## Baseline Modes

Default worktree analysis compares tracked changes against `HEAD` and records untracked files in the special appendix:

```sh
python3 skills/analyze-diff/scripts/build_file_change_table.py
```

Commit range uses direct `base..target` semantics:

```sh
python3 skills/analyze-diff/scripts/build_file_change_table.py --base <commit> --target <commit>
git log --oneline <commit>..<commit>
git diff --stat <commit>..<commit>
```

Branch compare uses `merge-base(base,target)..target`:

```sh
python3 skills/analyze-diff/scripts/build_file_change_table.py --base-branch main --target-branch feature
git merge-base main feature
git log --oneline <merge-base>..feature
git diff --stat <merge-base>..feature
```

## Evidence Commands

Collect the minimum useful context:

```sh
git status -sb
git branch --show-current
git diff --stat <range>
git diff --name-status -M <range>
git diff --numstat -M <range>
```

For large diffs, start with the generated CSV:

```sh
python3 - <<'PY'
import pandas as pd
df = pd.read_csv(".local/diff/file-change-table.csv")
print(df.sort_values("changed_lines", ascending=False).head(20))
print(df.groupby("file_extension")["changed_lines"].sum().sort_values(ascending=False))
PY
```

Then inspect representative or high-risk files:

```sh
git diff <range> -- path/to/file
git diff <range> --stat -- path/to/subsystem
```

For default worktree mode, omit `<range>` and use `HEAD` where a command requires a baseline.

## Special Files

Check `.local/diff/special-files.csv` before concluding that changes are low risk. Renames, binary files, copies, and untracked files can hide meaningful release impact.

When a worktree rename is unstaged, git may show it as a deletion plus an untracked file instead of a rename. Treat that as evidence state, not a script failure.

## Release Note Evidence

Use commit messages as intent signals, not proof:

```sh
git log --oneline <range>
```

Summarize themes from commit logs, then verify against file paths and representative hunks before writing user-facing claims.
