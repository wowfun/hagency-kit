# Hagency Kit

Language: English | [简体中文](README.zh-CN.md)

Practical agent skills for reviewing, diagnosing, and operating AI-assisted engineering work.

## Hagency CLI

The `hagency` CLI manages Hagency workspaces, sources, skill discovery, profiles, and generated `.agents/skills` profile outputs. Source registry entries live in [`hagency-config.toml`](hagency-config.toml), and profile configs live under `profiles/<name>/config.toml`.

```sh
uv tool install -e tools/hagency-cli
hagency s add <git-url> --sync
hagency s sync --profile <profile>
hagency skill ls
hagency p init -p <target> <profile>
```

`[defaults].depth` sets the default sync depth; transient Git clone/fetch/pull failures are retried automatically. Use `hagency source sync -s <slice>` to resume a selected source range after a failure. When a Git URL's inferred repo name already exists, `source add` falls back to `owner/repo`; pass `--name` to choose a custom source name.

## Skills

| Skill | When | What it does |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | Explaining git diffs, commit ranges, branch comparisons, or pasted changesets | Turns raw change evidence into release-oriented summaries, feature change lists, risk notes, testing gaps, and draft release notes. |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | Auditing prompts, agent workflows, toolchains, multi-agent systems, or production readiness | Scores workflow health across prompts, context, tools, architecture, safety, reliability, and system performance using available evidence. |
| [`eval-skill-quality`](skills/eval-skill-quality/SKILL.md) | Reviewing or preparing a skill before publishing | Evaluates skill quality, activation reliability, semantic clarity, SRL reliability, leakage risk, maintainability, and real-world value. |
| [`git-collab-flow`](skills/git-collab-flow/SKILL.md) | Managing `dev`, `feat-*` / `dev-*`, and `local-*` branch workflows | Produces safe git command sequences for syncing mainline updates, rebasing feature branches, cherry-picking public commits, and keeping PR history clean. |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | Using the Hagency Kit CLI for sources, profiles, skill discovery, or profile initialization | Helps agents inspect and manage `hagency` workspace sources, profile skill selectors, source syncs, and generated profile skill outputs. |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | Investigating application, server, JSON, CI, or rotated gzip logs | Samples and analyzes logs to explain failures, error spikes, slow requests, traffic patterns, and incident signals while keeping evidence bounded and redacted. |

## Profiles

A profile is a lightweight bundle definition for an agent workflow scene.

A profile lists the source names and skill selectors it enables in `profiles/<name>/config.toml`. After initialization, selected skills are materialized into a target workspace under `.agents/skills`.
