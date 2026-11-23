# Hagency CLI

`hgc` manages Hagency workspaces, tools, skills, profiles, file transfers, cleanup, and the local model proxy. Use `--help` at any level for offline usage and options.

Warnings and errors go to stderr. On a terminal, warnings appear in yellow and errors in red. Redirect stderr or set `NO_COLOR` for plain text.

## Install and get started

Requires Python 3.11 or newer. From the Hagency Kit repository root:

```sh
uv tool install -e tools/hagency-cli
hgc --help
hgc skill list --source workspace
hgc skill add workspace:skills/analyze-diff --dry-run
hgc skill add workspace:skills/analyze-diff
```

`skill ls` prints one `SOURCE:selector` reference per line. Pass a reference to `skill add` to install it in `./.agents/skills`. Use `--dir /path/to/project` for another project or `--global` for `~/.agents/skills`.

The command tree is:

```text
hgc init
hgc ln -s TARGET [DEST]
hgc source  list|show|add|remove|sync
hgc skill   list|add
hgc tool    list|add|remove|install|upgrade|uninstall
hgc profile list|show|add|update|remove|apply
hgc file    init|push|pull|sync|pack|apply|rule|purge|clean
hgc service model-proxy start|stop|restart
```

Use `ls` for `list`, `a` for `add`, `rm` for `remove`, and `i` for `install`. Tool upgrades also accept `update` and `u`. Profile edits use `profile update/u`. `profile a` creates a profile. To apply one, use `profile apply/install/i`. Skill installation also supports `skill add/a`. Help and completion include every alias.

Option aliases likewise share a row. Use `--install/-i` on `tool add` and `--update/-u` on one-way file transfers and bundle application. `source sync` updates Git checkouts and `file sync` synchronizes project files over SFTP.

Use `hgc ln -s TARGET [DEST]` to create a link from any directory. `DEST` defaults
to the current directory. An existing destination directory receives a link
named after `TARGET`. Relative targets are interpreted from the final link's
parent directory. For example,
`hgc ln -s ../assets public` creates `public/assets` pointing to the sibling
`assets` directory when `public` already exists.

On native Windows, including PowerShell and Git Bash, directory targets use
PowerShell junctions. File targets use native symlinks and may require Windows
Developer Mode or an elevated shell. Windows requires the target to exist.
Linux, macOS, and WSL use symlinks and allow dangling targets.

Install shell completion or print its script:

```sh
hgc --install-completion
hgc --show-completion bash
```

Completion covers commands, aliases, options, directories, and locally available source, profile, skill, and selector values. It respects workspace discovery and `--checkout-dir`; missing, invalid, unreadable, or unsynced workspace data is silently omitted. Completion performs no network access or writes.

For a non-editable installation, use `uv tool install tools/hagency-cli`, or install a built wheel. Such installations require an explicit `--root` when run outside a Hagency workspace. The packaged help works offline, and `hgc --help` links to the online guides.

## Documentation

| Guide | Contents |
| --- | --- |
| [Directories and configuration](docs/configuration.md) | Workspace discovery, TOML configuration, and installation destinations. |
| [Sources](docs/sources.md) | Register, inspect, sync, and remove local or Git sources. |
| [Skills](docs/skills.md) | Discover, select, and install skills from workspace and external sources. |
| [Tools](docs/tools.md) | Check PATH status with `ls`, read declarations with `--no-probe`, require availability with `--strict`, and manage installations. |
| [Profiles](docs/profiles.md) | Create reusable skill selections and apply them to a project. |
| [SFTP file sync](docs/file-sync.md) | Initialize SFTP configuration and push, pull, or synchronize project files. |
| [Offline sync bundles](docs/sync-bundles.md) | Pack, verify, and apply portable ZIP snapshots or Git patches. |
| [Project artifact purge](docs/purge.md) | Find and remove rebuildable artifacts, with scan-root and selection controls. |
| [Agent and cache cleanup](docs/clean.md) | Clean project paths, caches, and supported Agent versions or staging files. |
| [Cleanup rules](docs/cleanup-rules.md) | Configure expressions, filters, retention, scan budgets, and deletion checks. |
| [Local model proxy](docs/model-proxy.md) | Configure providers and hooks, and manage the background service. |
| [Troubleshooting](docs/troubleshooting.md) | Resolve common failures and compare dry-run and exit-status behavior. |
| [Development](docs/development.md) | Understand module ownership, maintain command help, and validate changes. |
