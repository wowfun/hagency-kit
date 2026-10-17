# Project artifact purge

[CLI guide](../README.md) | [Agent and cache cleanup](clean.md) | [Cleanup rules](cleanup-rules.md)

`hgc file purge` finds rebuildable project artifacts such as dependency directories, build output, test and tool caches, and directories carrying a valid `CACHEDIR.TAG`. Start with a preview:

```sh
hgc file purge --dry-run
hgc file purge
hgc file purge ~/Work/client-a ~/scratch/project-b
hgc file purge ./project --recursive --dry-run
hgc file purge ./cache --include-unverified --dry-run
hgc file purge --paths
```

Windows supports both purge previews and permanent deletion, with the same Git, link, junction, mount and filesystem-identity protections.

Candidate discovery defaults to **depth 6**, with each scan root at depth 0. `-R` / `--recursive` removes this limit. Discovery uses an explicit stack, deduplicates directory identities, prunes child mount points (including Linux bind mounts), and never descends into whole artifact candidates, even when they are protected. Automatic home-root discovery still checks project markers within two levels.

Positional `PATH...` values are temporary scan roots for that invocation and replace both the saved path list and automatic discovery. Without positional paths, a nonempty per-user path file replaces automatic discovery; a missing or effectively empty file restores the automatic roots. `--paths` is exclusive with positional paths, `--dry-run`, `--recursive`, `--include-unverified`, `--config`, `--rule`, `--scan-timeout`, and `--yes`. Use it by itself to create or edit this file, show which configured roots exist, and reopen the effective list after the editor exits. It uses `$VISUAL`, then `$EDITOR`, then `notepad.exe` on Windows, `open -W -t` on macOS, or `vi` elsewhere. Blank lines and `#` comments are ignored, and each entry must be an absolute or `~` path.

The path file is `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths` on Linux and WSL, `~/Library/Application Support/Hagency/space-purge-paths` on macOS, and `%APPDATA%\Hagency\space-purge-paths` on Windows. Windows falls back to `~/.config/hagency/space-purge-paths` when `APPDATA` is unavailable.

Automatic discovery checks existing `~/www`, `~/dev`, `~/Projects`, `~/GitHub`, `~/Code`, `~/Workspace`, `~/Repos`, `~/Development`, `~/.codex/worktrees`, and `~/.claude/worktrees`, plus direct children of the home directory that contain project markers within two levels. It does not automatically search system or cloud-storage roots. A worktree location alone does not qualify a directory for deletion: by default, candidates must match an active project rule or a valid `CACHEDIR.TAG`, have enclosing project evidence, and pass Git and filesystem checks. Configured rules can also select ordinary files. Being a Git worktree is not a blanket exclusion; Git protection excludes candidates containing tracked files. Inspect the exact candidate paths in the preview.

`--include-unverified` additionally admits rule-matched files and directories without enclosing project markers, non-Composer `vendor` directories, and `bin` directories without confirmed .NET output. Preview labels each reason: `project context unverified` and/or `artifact purpose unverified`, including missing Composer or .NET evidence. These candidates are never preselected, regardless of age; select them manually in a TTY, then confirm deletion, or explicitly combine `--include-unverified` with `--yes` to include them in automatic deletion. Without this option, protection messages state the missing evidence. Global Xcode `DerivedData`, Git/link/mount/Agent protections, exclusions and age gates still apply. It changes neither automatic root discovery nor the depth limit; use explicit `PATH...` and `-R` as needed. It cannot accompany `--paths`.

In a TTY, Questionary presents a multi-select list. Verified candidates whose last artifact activity is strictly more than seven days old are selected by default; recent or uncertain candidates remain unselected. A normal purge then prints every selected absolute path and asks for a second confirmation that defaults to No. With `--dry-run`, the multi-select still opens, but the selected entries are only previewed and the destructive confirmation is skipped. Without `--yes`, a non-TTY run skips the TUI and previews all candidates with their default selection status, even when `--dry-run` is omitted. `--yes/-y` selects all eligible candidates after scanning and skips selection and confirmation in both TTY and non-TTY invocations; `--dry-run` still prevents deletion.

Purge deletes selected artifacts permanently rather than moving them to Trash or the Recycle Bin. It excludes Git-tracked candidates, including an entire candidate that contains a nested Git repository with tracked files, plus symlinks, junctions, reparse paths, mount points, directories with zero estimated disk usage selected only by `purge.artifacts` / `purge.cachedirs`, and the global Xcode `DerivedData` directory. Explicit custom rules can select these zero-size directories. Missing project evidence, non-Composer `vendor` directories and `bin` directories outside `.NET` projects require an explicit `--include-unverified`. If permanent deletion fails partway through because of permissions, a filesystem change, or another I/O error, some contents may already be gone; the command reports the failure, continues with later selections, and exits with status 1.
