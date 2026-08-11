# Hagency Kit

Language: English | [简体中文](README.zh-CN.md)

Practical agent skills for reviewing, diagnosing, and operating AI-assisted engineering work.

## Hagency CLI

The `hgc` CLI manages Hagency workspaces, sources, skill discovery and installation, profiles, and generated profile skill outputs. Source registry entries live in [`hagency-config.toml`](hagency-config.toml), and profile configs live under `profiles/<name>/config.toml`.

```sh
uv tool install -e tools/hagency-cli
hgc s add <git-url> --sync
hgc s sync --profile <profile>
hgc skill ls
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace>
hgc skill add <source>:<selector> --global
hgc p init -p <xxx>/skills <profile>
hgc p init -d <workspace> <profile>
```

Install completion for the current shell, or print a shell-specific completion script:

```sh
hgc --install-completion
hgc --show-completion bash
```

Completion covers commands, aliases, options, directories, and locally available source, profile, skill, and selector values. It respects the current directory, `--root`, and `--checkout-dir`; missing, invalid, unreadable, or unsynced workspace data is silently omitted.

`[defaults].depth` sets the default sync depth; transient Git network failures are retried automatically. Use `hgc source sync -s <slice>` to resume a selected source range after a failure. When a Git URL's inferred repo name already exists, `source add` falls back to `owner/repo`; pass `--name` to choose a custom source name.

Use an optional Windows checkout override when the same config is shared across platforms:

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Checkout directory precedence is `--checkout-dir`, then `checkout_dir_windows` on native Windows, then `checkout_dir`. WSL is not treated as native Windows and continues to use `checkout_dir`. On native Windows, Git Bash paths such as `/d/Projects/references` are normalized to `D:/Projects/references`. If `checkout_dir_windows` is omitted, native Windows falls back to `checkout_dir`. This is a config-only override; there is no new CLI flag.

Normal sync refuses non-fast-forward updates. If an upstream source rewrites history and its checkout is disposable, rerun the failed selection with `--reanchor`. Reanchoring requires a checkout with no staged, unstaged, or untracked changes, then replaces local-only commits with the fetched upstream history. The option works with source names, `--profile`, and `--slice`; `--dry-run` only describes the conditional behavior.

`skill add` installs one discovered skill by unique name or exact `SOURCE:selector`. It links into the invocation directory's `.agents/skills` by default. Use `-p/--path` for an exact skills container, to which only the skill name is appended; use `-d/--dir` for a workspace root whose destination is `<workspace>/.agents/skills`; or use `--global` for `~/.agents/skills`. These three options are mutually exclusive. Relative destination paths resolve against the invocation directory, and `~` expands to the current user's home. `--root` and `--checkout-dir` affect skill discovery only; they never change the installation destination. Non-Windows platforms use symlinks; Windows uses junctions.

`profile init` requires exactly one destination: `-p/--path` is the final skills container and `-d/--dir` is a workspace root that expands to `<workspace>/.agents/skills`. Destination paths follow the same invocation-directory and `~` expansion rules, while `--root` and `--checkout-dir` remain discovery-only. This changes the previous `-p` behavior. Migrate `hgc p init -p <root> <profile>` to `hgc p init -d <root> <profile>`.

## Skills

| Skill | When | What it does |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | Explaining git diffs, commit ranges, branch comparisons, or pasted changesets | Turns raw change evidence into release-oriented summaries, feature change lists, risk notes, testing gaps, and draft release notes. |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | Auditing prompts, agent workflows, toolchains, multi-agent systems, or production readiness | Scores workflow health across prompts, context, tools, architecture, safety, reliability, and system performance using available evidence. |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | Using the Hagency Kit CLI for sources, profiles, skill discovery or installation, or profile initialization | Helps agents inspect and manage Hagency workspace sources, direct skill installs, profile skill selectors, source syncs, and generated profile skill outputs. |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | Investigating application, server, JSON, CI, or rotated gzip logs | Samples and analyzes logs to explain failures, error spikes, slow requests, traffic patterns, and incident signals while keeping evidence bounded and redacted. |

## Profiles

A profile is a lightweight bundle definition for an agent workflow scene.

A profile lists the source names and skill selectors it enables in `profiles/<name>/config.toml`. After initialization, selected skills are materialized in the requested skills container; `-d/--dir` uses the workspace's `.agents/skills` container.
