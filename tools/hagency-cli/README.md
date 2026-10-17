# Hagency CLI

`hgc` manages Hagency workspaces, skill sources, skill installation, reusable profiles, project file transfers, artifact cleanup, and a local model proxy. Each command's `--help` includes its prerequisites, defaults, effects, and examples, including when no workspace is available.

## Install and get started

Requires Python 3.11 or newer. From the Hagency Kit repository root:

```sh
uv tool install -e tools/hagency-cli
hgc --help
hgc skill list --source workspace
hgc skill add workspace:skills/analyze-diff --dry-run
hgc skill add workspace:skills/analyze-diff
```

The last command links the skill into the invocation directory's `.agents/skills`. Use `--dir /path/to/project` for another project's skills container, or `--global` for your user's `~/.agents/skills`. Inspect each command before running it:

```sh
hgc source --help
hgc source sync --help
hgc profile apply --help
hgc file push --help
hgc service model-proxy --help
```

The command tree is:

```text
hgc init
hgc source  list|show|add|remove|sync
hgc skill   list|add
hgc profile list|show|add|update|remove|apply
hgc file    init|push|pull|sync|pack|apply|purge|clean
hgc service model-proxy start|stop|restart
```

Help lists command aliases on the same row as their full names: `source, s`, `profile, p`, `list, ls`, `remove, rm`, and `update, u`. Both forms accept the same parameters and show the same full help. Option aliases likewise share a row, such as `-p, --path PATH`. `source sync` updates Git checkouts; `file sync` synchronizes project files over SFTP.

Install shell completion or print its script:

```sh
hgc --install-completion
hgc --show-completion bash
```

Completion covers commands, aliases, options, directories, and locally available source, profile, skill, and selector values. It respects workspace discovery and `--checkout-dir`; missing, invalid, unreadable, or unsynced workspace data is silently omitted. Completion performs no network access or writes.

For a non-editable installation, use `uv tool install tools/hagency-cli`, or install a built wheel. Such installations require an explicit `--root` when run outside a Hagency workspace. The packaged help works without access to this repository or README.

## Documentation

| Guide | Contents |
| --- | --- |
| [Directories and configuration](docs/configuration.md) | Workspace discovery, TOML configuration, and installation destinations. |
| [Sources](docs/sources.md) | Register, inspect, sync, and remove local or Git sources. |
| [Skills](docs/skills.md) | Discover, select, and install skills from workspace and external sources. |
| [Profiles](docs/profiles.md) | Create reusable skill selections and apply them to a project. |
| [SFTP file sync](docs/file-sync.md) | Initialize SFTP configuration and push, pull, or synchronize project files. |
| [Offline sync bundles](docs/sync-bundles.md) | Pack, verify, and apply portable ZIP snapshots or Git patches. |
| [Project artifact purge](docs/purge.md) | Find and remove rebuildable artifacts, with scan-root and selection controls. |
| [Agent and cache cleanup](docs/clean.md) | Clean project paths, caches, and supported Agent versions or staging files. |
| [Cleanup rules](docs/cleanup-rules.md) | Configure expressions, filters, retention, scan budgets, and deletion checks. |
| [Local model proxy](docs/model-proxy.md) | Configure providers and hooks, and manage the background service. |
| [Troubleshooting](docs/troubleshooting.md) | Resolve common failures and compare dry-run and exit-status behavior. |
| [Development](docs/development.md) | Understand module ownership, maintain command help, and validate changes. |
