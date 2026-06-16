# Hagency Kit

Language: English | [简体中文](README.zh-CN.md)

Practical agent skills for reviewing, diagnosing, and operating AI-assisted engineering work.

## Skills

| Skill | When | What it does |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | Explaining git diffs, commit ranges, branch comparisons, or pasted changesets | Turns raw change evidence into release-oriented summaries, feature change lists, risk notes, testing gaps, and draft release notes. |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | Auditing prompts, agent workflows, toolchains, multi-agent systems, or production readiness | Scores workflow health across prompts, context, tools, architecture, safety, reliability, and system performance using available evidence. |
| [`eval-skill-quality`](skills/eval-skill-quality/SKILL.md) | Reviewing or preparing a skill before publishing | Evaluates skill quality, activation reliability, semantic clarity, SRL reliability, leakage risk, maintainability, and real-world value. |
| [`git-collab-flow`](skills/git-collab-flow/SKILL.md) | Managing `dev`, `feat-*` / `dev-*`, and `local-*` branch workflows | Produces safe git command sequences for syncing mainline updates, rebasing feature branches, cherry-picking public commits, and keeping PR history clean. |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | Investigating application, server, JSON, CI, or rotated gzip logs | Samples and analyzes logs to explain failures, error spikes, slow requests, traffic patterns, and incident signals while keeping evidence bounded and redacted. |

## Profiles

A profile is a lightweight bundle definition for an agent workflow scene.

External skill sources are registered in [`skills/config.toml`](skills/config.toml). A profile lists the source names it enables in `profiles/<name>/config.toml`; generated `.agents/skills/` links are ignored by git. Sync remote sources first, then initialize the profile into a target workspace.

```sh
uv tool install -e tools/hagency-cli
hagency skill -se --profile content --dry-run
hagency profile init -p ~/workspaces/content content --dry-run
```
