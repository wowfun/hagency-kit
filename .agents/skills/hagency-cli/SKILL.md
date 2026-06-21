---
name: hagency-cli
description: Use the Hagency Kit CLI for workspace, source, profile, and profile skill operations. Trigger for `hagency source`, `hagency profile`, source syncs, profile initialization, `.agents/skills` links, `hagency-config.toml`, and `profiles/*/config.toml` work.
---

# Hagency CLI

Use the repo-local `hagency` CLI to manage Hagency workspaces, sources, profiles, and generated profile skill links. If the CLI cannot satisfy the user's request, explain the gap and ask whether to improve hagency-cli.

- Use `--root/-r <path>` when the workspace root is not the current directory or an ancestor containing `hagency-config.toml`.
- Manage sources with `hagency source list/show/add/remove/sync`; `list` and `remove` also support `ls` and `rm`.
- Manage profiles with `hagency profile list/show/add/update/remove/init`; `list` and `remove` also support `ls` and `rm`.
- Add or merge a profile skill source with `hagency profile update <profile> -AS <source>`.
- Remove a profile skill source with `hagency profile update <profile> -RS <source>`.
- Use `--include` and `--exclude` selectors with `-AS/--add-skill`; omitted include means all discovered `SKILL.md` directories from that source.
- Initialize profile skills with `hagency profile init -p <target> <profile>`; it creates symlinks by default.
- Use `hagency profile init -p <target> <profile> -cp` when the target workspace should get independent skill copies that can evolve separately from the source.
- Prefer `--dry-run` before commands that mutate checkouts, profile configs, source configs, files, symlinks, or copied skill directories.
- Treat `.agents/skills` entries as generated profile outputs.

Common examples:

```sh
hagency source ls --root /path/to/workspace
hagency source add https://example.invalid/acme/ExamplePack.git --root /path/to/workspace --dry-run
hagency source sync --profile content --depth 1 --root /path/to/workspace --dry-run
hagency profile ls --root /path/to/workspace
hagency profile add content --description "Content creation profile." -AS ExamplePack --root /path/to/workspace --dry-run
hagency profile update content -AS ExamplePack --include hunt write --exclude draft --root /path/to/workspace --dry-run
hagency profile update content -RS ExamplePack --root /path/to/workspace --dry-run
hagency profile init -p /path/to/target content --root /path/to/workspace --dry-run
hagency profile init -p /path/to/target content --root /path/to/workspace -cp --dry-run
hagency profile init -p C:\Users\me\workspace content --root C:\Users\me\hagency-kit --dry-run
hagency profile init -p /c/Users/me/workspace content --root /c/Users/me/hagency-kit --dry-run
```
