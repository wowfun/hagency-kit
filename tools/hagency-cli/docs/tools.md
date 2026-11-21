# Tools

Declare tools in `hagency-config.toml`. Profiles inherit the workspace catalog. A table with the same name in `profiles/NAME/config.toml` overrides individual fields. Arrays replace the inherited array. Profiles can add their own entries and exclude inherited entries with `enabled = false`.

```toml
[tool.playwright]
install = ["pnpm", "npm"]

[tool.ffmpeg]

[tool.markitdown]
install = ["uv"]
package = "markitdown[pdf,docx,pptx]"
```

`command` and `package` default to the tool name. `version` accepts an exact version, PEP 440 comparisons such as `">=1.50,<2"`, or `"*"`. Omitting it accepts any version. `version_args` defaults to `["--version"]`, or `["-version"]` for FFmpeg. `install_args` is an argv array. Each string is one argument. The selected manager receives these arguments on install and upgrade.

```sh
hgc tool ls
hgc tool check --profile dev
hgc tool a ffmpeg --profile dev --version ">=7,<10"
hgc tool rm ffmpeg --profile dev
hgc tool i playwright --install-dir ./tools
hgc tool update markitdown -- --offline
hgc tool uninstall playwright --install-dir ./tools
hgc profile i dev -T --dry-run
```

`list` reads declarations. `check` reports the executable selected by the current PATH, its version and availability. It exits with status 1 if any requested command is missing, fails to launch, or cannot satisfy its version constraint. A working command whose version is unrecognized satisfies an unconstrained declaration.

`add` updates the supplied fields. Use repeated `--install`, `--version-arg` and `--install-arg` options for arrays. `remove` edits the catalog. In a profile it disables an inherited tool or deletes a profile-only entry. `uninstall` removes the software and keeps its declaration. Use `--dry-run` to preview edits and lifecycle operations.

## Installation

The installer option on `tool add` accepts `--install/-i`. For example, `hgc tool a playwright -i pnpm -i npm` saves the ordered preferences.

Install ensures a compatible version. New installations select the latest stable release available from the selected source that meets the constraint. Upgrade selects a compatible release again. The selected manager handles package dependencies. Playwright uses the npm package named `playwright`. Run `playwright install` separately when browser downloads are needed. MarkItDown installs the configured PDF, Word and PowerPoint extras, including when ensuring an existing base installation.

Hagency selects the first available manager in the declared order. Installer output appears as it runs. An omitted method selects the platform package manager.

| Manager | Installation behavior |
| --- | --- |
| pnpm, npm | Install a global Node.js command. |
| uv | Install an isolated Python tool with its declared extras. |
| WinGet | Select the exact package ID on Windows. The FFmpeg ID is `Gyan.FFmpeg`. |
| Homebrew | Select the matching macOS formula. |
| apt, dnf, pacman | Use the distribution package. Interactive terminals can authenticate with sudo. Unattended runs need an existing credential or passwordless sudo. |
| Portable FFmpeg | For a new installation whose version or destination the native manager cannot provide, download and verify a Gyan Windows x64 build or a Martin Riedl Linux/macOS amd64 or arm64 build. Upgrades keep the selected installation and its manager. |

New Windows installations default to `D:\hagency\<username>\tools`. When the default location is unavailable they use `$XDG_DATA_HOME/hagency/tools`, defaulting to `~/.local/share/hagency/tools`. The executable directory is added to the current user's PATH for new terminals. Other platforms use the manager's global paths. An explicit `--install-dir` selects a separate installation even when another version is on PATH. Use the reported command path directly, or put its directory on PATH. System package managers that cannot relocate a generic package report the unsupported destination.

When the current shell resolves a different executable or has no matching command, `install` shows both the verified `Installed:` path and the current `PATH:` result, including when it reuses an existing installation. In an open Git Bash session, prepend the reported executable directory to use it immediately. For MarkItDown in the default Windows location, run:

```bash
export PATH="/d/hagency/$USERNAME/tools/bin:$PATH"
hgc tool check markitdown
```

`upgrade` and `uninstall` can act on external installations when the manager identifies the package and its matching executable. Operations remember the installation root, manager and executable under the user's Hagency state directory. Use `--install-dir` to choose among multiple recorded copies. The final PATH check shows which copy the current shell resolves.

Use `profile apply/install/i NAME --with-tools/-T` to apply skills and then ensure the effective tool catalog. The command attempts every selected tool and reports all failures together. With the default flags only skills are applied. `--install-dir` controls tools independently of the skills destination.

On Windows, uninstall also removes a PATH entry added by Hagency when the directory has no remaining commands or recorded tools.

If an installation was replaced manually or its manager can no longer establish ownership, use `hgc tool uninstall NAME --forget` to discard the local receipt. Files and PATH entries stay in place. This also works after removing the catalog declaration. With multiple receipts, specify `--install-dir PATH` to identify the one to forget. Add `--dry-run` to preview the selected receipt.

Separate tools can install concurrently. A second operation on the same tool, package, or command reports the conflict and can be retried after the first one finishes.
