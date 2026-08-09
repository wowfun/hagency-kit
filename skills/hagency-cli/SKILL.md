---
name: hagency-cli
description: Use the Hagency Kit CLI for workspace, source, skill, and profile workflows. Trigger for `hagency` or `hgc`, source syncs, skill discovery or installation, profile skill edits, profile initialization, `hagency-config.toml`, `profiles/*/config.toml`, generated profile skill outputs, and updates to `skills/hagency-cli/SKILL.md`.
---

# Hagency CLI

Use the repo-local `hagency` CLI to inspect and manage Hagency workspaces, sources, skills, profiles, and generated profile skill links. `hgc` is the short alias for the same interface. If the CLI cannot satisfy the user's request, explain the gap and ask whether to improve `hagency-cli`.

## Workspace Context

Resolve the workspace from the current directory when it is inside a tree with `hagency-config.toml`. Use `-r` when the workspace root is elsewhere. Source registry entries live in `hagency-config.toml`.

Generated profile output belongs in the selected skills container. `-p/--path` selects that container directly; `-d/--dir` selects a workspace and uses its `.agents/skills` container. Treat generated entries as links or copies, not source skill files.

For source checkout discovery, the existing `--checkout-dir` option has highest precedence. Without it, native Windows uses the optional `[defaults].checkout_dir_windows`; other platforms, including WSL, use `[defaults].checkout_dir`. Native Windows also falls back to `checkout_dir` when the override is absent. This does not add a CLI flag.

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Native Windows normalizes Git Bash paths such as `/d/Projects/references` to `D:/Projects/references`. Keep the base `checkout_dir` usable by Linux, macOS, and WSL rather than putting a Windows-only path there.

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

Normal sync refuses non-fast-forward updates. When the error lists a `--reanchor` retry command, use it only if every selected checkout is disposable and may lose local-only commits.

```sh
hagency s sync <source> --reanchor -r <root>
hagency s sync --profile <profile> -s 4: --reanchor -r <root>
```

Reanchoring requires no staged, unstaged, or untracked changes. It does not save sync state or create recovery refs. `--dry-run --reanchor` only describes what an actual sync may do after fetching.

## Install One Skill

Use `skill add` with a unique discovered skill name or an exact `SOURCE:selector`. The default destination is the invocation directory's `.agents/skills`. Use `-p/--path` for an exact skills container, `-d/--dir` for a workspace whose destination is `<workspace>/.agents/skills`, or `--global` for the current user's `~/.agents/skills`. These three options are mutually exclusive. Relative destinations resolve against the invocation directory, and `~` expands to the current user's home. `-r/--root` and `--checkout-dir` change source discovery only, never the destination.

```sh
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace> -r <root>
hgc skill add <skill> --global -r <root>
hgc skill add <skill> --dry-run
```

The command installs exactly one skill. If a name is ambiguous, use one of the exact references shown in the error. Source-only and multi-match references are rejected. Installation uses symlinks except on Windows, where it uses junctions.

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

Use `p init` to materialize profile-selected skills. The command requires exactly one destination option: `-p/--path` names the final skills container and appends only each skill name, while `-d/--dir` names a workspace root and writes to `<workspace>/.agents/skills`. Relative destinations resolve against the invocation directory, and `~` expands to the current user's home. These rules are independent of `-r/--root` and `--checkout-dir`, which only control profile and source discovery. Symlinks are the default except on Windows, where junctions are the default. Use `-cp` when the target should get independent copies that can evolve separately from the source.

```sh
hagency p init -p <xxx>/skills <profile> -r <root>
hagency p init -d <workspace> <profile> -r <root>
hagency p init -p <xxx>/skills <profile> -r <root> -cp
hagency p init -d <windows-workspace> <profile> -r <windows-root>
hagency p init -d <git-bash-workspace> <profile> -r <git-bash-root>
```

This is a breaking change to `-p`: migrate `hgc p init -p <root> <profile>` to `hgc p init -d <root> <profile>`. The CLI does not append `.agents/skills` to a `--path` value; use `--dir` when the input is a workspace root.

## Safety and Boundaries

- Prefer `--dry-run` before commands that mutate checkouts, profile configs, source configs, files, symlinks, or copied skill directories.
- Do not create `agents/openai.yaml` for this repo-local skill unless the user explicitly asks for it.
