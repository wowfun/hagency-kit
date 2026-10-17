# Agent and cache cleanup

[CLI guide](../README.md) | [Project artifact purge](purge.md) | [Cleanup rules](cleanup-rules.md)

`hgc file clean` supports project, `cachedir-tag`, and generic `paths` rules on **Linux/WSL and Windows**. Agent version and staging sources require **Linux/WSL**. By default, automatic rules include Codex, Claude Code, and Copilot, plus configured `paths` rules with `auto_apply = true`:

```sh
hgc file clean --dry-run
hgc file clean --agent codex --agent claude
hgc file clean --config ./hagency-cleanup.toml --dry-run
hgc file clean -r project_default ./ --dry-run
```

On Windows, `clean` requires explicit `--rule/-r` selection of project/path rules: the builtin automatic rules include Agent sources, so a bare `hgc file clean` fails. If any selected branch uses an Agent source, including one selected through `-r auto`, the entire operation stops before scanning with `unsupported_platform`, lists up to five incompatible rules and sources (with an omitted count and shortened long labels), and exits with status 1. Supported rules are not partially executed in that invocation. macOS clean support remains unavailable.

`clean.project_default` is defined in this repository's `hagency-cleanup.toml`, not in the packaged defaults. A wheel installation outside a Hagency workspace can use `purge` for project artifacts or explicitly select the builtin `clean.empty_directories` rule. Additional clean project/path rules require a repository configuration or `--config`.

```sh
hgc file purge ./ --dry-run
hgc file clean -r empty_directories ./ --dry-run
```

Repeat `--agent codex|claude|copilot` to select Agent sources. This does not disable automatic generic path branches. `--agent` and `--rule` are mutually exclusive. Inspection reads local files and same-user `/proc` state; it never starts an Agent or installer and never connects to the network. If an eligible version disappears after a complete inventory, only that candidate is skipped; an incomplete inventory or uncertain source state prevents cleanup of that source. Each Agent source has an independent 60-second scan budget, checked during traversal, process inspection and bounded Git queries. Generic paths/project scanning is unlimited by default. Operating-system filesystem calls can still block independently of these checkpoints.

| Builtin rule | Recognized source and protection |
| --- | --- |
| `clean.codex.versions` | `$CODEX_HOME/packages/standalone/releases` (`CODEX_HOME` defaults to `~/.codex`). Recognizes stable Linux package manifests and legacy standalone layouts. Preserves `current`, `codex` and `codex-code-mode-host` launcher references, and `auto-update-version` references. Launchers use `CODEX_INSTALL_DIR` (default `~/.local/bin`) and `PATH`. Versions for another platform remain protected. |
| `clean.claude.versions` | Native version files in `~/.local/share/claude/versions`; `~/.local/bin/claude` must identify the current version. Unverifiable custom launchers stop this source. `CLAUDE_CONFIG_DIR` changes protected configuration data, not the native installation location. |
| `clean.copilot.versions` | Complete `pkg/universal/<version>/app.js` packages under `COPILOT_CACHE_HOME`, `${XDG_CACHE_HOME:-~/.cache}/copilot`, `COPILOT_HOME`, and `~/.copilot`. Retention is calculated across all roots, including duplicate versions. Direct version references and recognizable `@github/copilot` package launchers are supported; unknown launchers are retained without probing them. |
| `clean.codex.staging` | Direct `.staging.*` directories under standalone `releases`; `openai-bundled.staging-*` under `$CODEX_HOME/.tmp/bundled-marketplaces`; `marketplace-add-*` / `marketplace-upgrade-*` under `$CODEX_HOME/.tmp/marketplaces/.staging`. Other names and plugin installations are untouched. |

Version rules retain current/referenced versions, **one earlier fallback version**, and pending newer versions by default. Prereleases, incomplete releases and unknown layouts are protected. `keep_previous` controls the number of distinct fallback versions; active version rules for the same source use the largest value. Codex fallback versions must match the current platform. Copilot conservatively protects the highest complete stable cache version and launcher references across both cache layouts.

Only Codex standalone uses a verified installer-lock protocol. Claude and Copilot rely on repeated version/launcher, process/occupancy and frozen-file checks; these are not a transaction with every external installer.

Staging directories and **all of their contents** must have modification times strictly more than seven days old. Configuration can increase this threshold but cannot reduce it. Running tools, open files, active working directories and uncertain process state prevent cleanup. Standalone Codex cleanup checks the existing installer lock during planning, then holds it across revalidation and deletion; it never creates or removes installer locks. A missing lock file prevents standalone cleanup because the command cannot synchronize with the installer. The [Codex installer](https://github.com/openai/codex/blob/main/scripts/install/install.sh) leaves the lock file in place after releasing its `flock`; installations using only its directory-lock fallback are conservatively skipped.

Sessions, authentication, configuration, plugin installations, worktree bodies and known Desktop data remain protected. Generic rules cannot bypass these protections by naming Agent directories, their contents or a containing directory. Purge can still remove qualifying project artifacts inside Agent worktrees. `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `COPILOT_HOME` also identify protected data locations; tool path overrides must be absolute and free of linked components; existing parent traversal is normalized before matching. Symlinked default data locations also protect their real destinations from generic rules.
