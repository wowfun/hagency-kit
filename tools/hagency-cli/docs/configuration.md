# Directories and configuration

[CLI guide](../README.md)

## Workspace discovery

Workspace commands resolve their Hagency root in this order:

1. Explicit `-r/--root`, which must contain `hagency-config.toml`.
2. The invocation directory and its ancestors, using the nearest `hagency-config.toml`.
3. The Hagency Kit source checkout supplying an editable installation, if its root config exists.

The editable fallback checks only that checkout's root. An ordinary wheel has no installation-directory fallback. `hgc init` always initializes its explicit `--root` or the invocation directory, creates that directory if necessary, and never uses workspace discovery:

```sh
hgc init --root ./kit --dry-run
hgc init --root ./kit
```

Initialization writes checkout and depth defaults. An existing config requires `--force`, which replaces it rather than merging. `--dry-run` prints the proposed configuration without writing.

File sync and bundle commands use the invocation directory or their project `--root` directly. They need no Hagency workspace. Purge uses its own [scan roots](purge.md). Purge and clean discover [cleanup rules](cleanup-rules.md) independently from those roots. The model proxy accepts an explicit `--config` without a workspace; otherwise its config is resolved under the Hagency root.

## Workspace and profile configuration

The source registry is `hagency-config.toml`. A minimal example with a local and a remote source:

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
depth = 1
remote_name = "origin"
remote_ref = "main"

[source.local-skills]
path = "./skills"

[source.example.remote]
url = "https://github.com/owner/repo.git"
ref = "main"
```

`[source.NAME].path` is an explicit local or checkout path; relative values resolve against the Hagency root. Without an explicit path, remote checkouts use `<checkout_dir>/<source-name>`. `--checkout-dir` overrides the checkout base, then native Windows uses `checkout_dir_windows` when present, then `checkout_dir` applies. WSL uses `checkout_dir`. Native Windows normalizes Git Bash paths such as `/d/Projects/references` to `D:/Projects/references`. There is no separate Windows CLI flag.

`remote_name` and `remote_ref` default to `origin` and `main`; per-source `remote.name` and `remote.ref` override them. `depth` is the default shallow sync depth, and `source sync --depth` overrides it. `hgc init` writes depth 1; omitting depth from an existing config leaves it unset. The built-in `workspace` skill source represents the Hagency root and does not need a registry entry.

Profiles live in `profiles/NAME/config.toml`. For example:

```toml
name = "review"
description = "Review changes"

[skill.workspace]
include = ["skills/analyze-diff"]
```

Each `[skill.SOURCE]` selects a source. Optional `include` and `exclude` lists contain source-relative skill or subtree selectors. Without includes, all discovered skills in the source are selected before exclusions. Use `hgc skill list` to inspect selectors; selectors must remain inside the source. Edit profiles through [profile commands](profiles.md).

## Installation destinations

Destinations are independent of source discovery. Relative destinations resolve against the invocation directory and `~` expands to the current user's home.

| Option | Destination |
| --- | --- |
| `-p/--path PATH` | The exact skills container, with only each skill name appended |
| `-d/--dir DIR` | `DIR/.agents/skills` |
| `skill add --global` | `~/.agents/skills` |
| `skill add` without destination options | `./.agents/skills` |

`skill add` accepts at most one destination option; `profile apply` requires exactly one of `--path` or `--dir`. `--root` and `--checkout-dir` locate profiles and sources rather than choosing an installation target. A newly registered remote source persists the resolved `skill add --checkout-dir` override; existing sources use it for that invocation only.
