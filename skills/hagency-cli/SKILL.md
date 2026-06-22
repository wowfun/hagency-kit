---
name: hagency-cli
description: Use the Hagency Kit CLI for workspace, source, skill, and profile workflows. Trigger for `hagency source`, `hagency skill`, `hagency profile`, source syncs, profile skill edits, profile initialization, `hagency-config.toml`, `profiles/*/config.toml`, generated `.agents/skills` outputs, and updates to `skills/hagency-cli/SKILL.md`.
---

# Hagency CLI

Use the repo-local `hagency` CLI to inspect and manage Hagency workspaces, sources, skills, profiles, and generated profile skill links. If the CLI cannot satisfy the user's request, explain the gap and ask whether to improve `hagency-cli`.

## Workspace Context

Resolve the workspace from the current directory when it is inside a tree with `hagency-config.toml`. Use `-r` when the workspace root is elsewhere. Source registry entries live in `hagency-config.toml`.

Generated profile output belongs under target-workspace `.agents/skills`. Treat those entries as generated links or copies, not source skill files.

## Inspect Sources, Skills, Profiles

Use `s` and `p` for the top-level source and profile aliases. Use `ls` for list commands. Use `hagency skill ls` to scan `SKILL.md` directories before editing profile selectors.

```sh
hagency s ls -r <root>
hagency s show <source> -r <root>
hagency skill ls -s workspace -r <root>
hagency skill ls -s <source> -r <root>
hagency skill ls -p <profile> -r <root>
hagency skill ls --checkout-dir <checkout-dir> -r <root>
hagency p ls -r <root>
hagency p show <profile> -r <root>
```

## Sync Sources

Sync remote sources before relying on profile initialization or skill-name inference. For profile-scoped sync, keep the long `--profile` option because `source sync -s` is already the slice selector. Use `--depth` for shallow checkouts and `-s` with 1-based indexes to resume a failed subset.

```sh
hagency s sync --profile <profile> --depth 1 -r <root>
hagency s sync <source> --depth 1 -r <root>
hagency s sync --profile <profile> -s 4: -r <root>
hagency s sync --profile <profile> -s 1,3: -r <root>
```

## Edit Profiles

Use `p add` for new profile configs and `p u` for profile updates. `-AS` adds or merges a source, skill name, or `SOURCE:selector`; `-RS` removes one. Use `-i` and `-e` for include and exclude selectors. Use `--replace` only when the existing entry should be rewritten.

```sh
hagency p add <profile> --description "Profile description." -AS <source> -r <root>
hagency p u <profile> -AS <source> -i <include-selector> -e <exclude-selector> -r <root>
hagency p u <profile> -AS <source>:<selector> --replace -r <root>
hagency p u <profile> -RS <source> -r <root>
hagency p rm <profile> -r <root>
```

Skill-name inputs can resolve to a source when the name is unique. If the CLI reports ambiguity, rerun with the `SOURCE:selector` form shown in the error.

## Initialize Profile Skills

Use `p init` to materialize profile-selected skills into a target workspace. Symlinks are the default. Use `-cp` when the target should get independent copies that can evolve separately from the source.

```sh
hagency p init -p <target> <profile> -r <root>
hagency p init -p <target> <profile> -r <root> -cp
hagency p init -p <windows-target> <profile> -r <windows-root>
hagency p init -p <git-bash-target> <profile> -r <git-bash-root>
```

## Safety and Boundaries

- Prefer `--dry-run` before commands that mutate checkouts, profile configs, source configs, files, symlinks, or copied skill directories.
- Do not create `agents/openai.yaml` for this repo-local skill unless the user explicitly asks for it.
