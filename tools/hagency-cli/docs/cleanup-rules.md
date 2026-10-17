# Cleanup rules

[CLI guide](../README.md) | [Project artifact purge](purge.md) | [Agent and cache cleanup](clean.md)

Purge and clean share `CleanupRule`, `CleanupCandidate`, `CleanupPlan`, `CleanupReport` and `CleanupUI` through `files/cleanup`. Rules combine [packaged defaults](../src/hagency_cli/files/cleanup/defaults.toml) with **one repository file** named [`hagency-cleanup.toml`](../../../hagency-cleanup.toml), beside `hagency-config.toml` and intended for Git tracking.

Both commands require stable filesystem identities for scan roots, candidates, and their contents. Directories whose filesystem reports a zero inode are not traversed; each affected directory is reported as an incomplete scan rather than silently deduplicated by its missing identity. A missing identity inside a whole-directory candidate also prevents deletion.

Both commands accept `--config PATH`. Without it, the nearest current-directory ancestor containing `hagency-config.toml` selects the workspace; otherwise an editable installation may select its source repository. Only that repository's `hagency-cleanup.toml` is read. A wheel outside a workspace uses builtins. A missing automatic rules file also uses builtins; a missing explicit file or an invalid discovered file stops before scanning. Workspace discovery does not parse `hagency-config.toml` business contents. The selected TOML is trusted configuration and may be a symlink; its target state is checked again before deletion. POSIX checks device, inode, mode, size, mtime and ctime against the state verified when reading the file; any change stops deletion. Windows checks device, inode, mode, size, mtime and creation time (`st_birthtime_ns` when available, otherwise a fixed zero), avoiding incompatible `stat()`/`fstat()` ctime values. Windows and filesystems without a stable inode also reread and hash the contents when that state is unchanged. Relative rule roots use the selected file's containing directory, including when the file is a symlink.

Cleanup rule discovery is independent of `purge PATH...` and the saved `space-purge-paths` list. The commands never search scan targets for rules and never read a user configuration directory for cleanup rules. Paths declared by custom rules resolve against the selected TOML's directory; `~` expands to the current home. Explicit scan roots and custom rule roots may be outside the home directory, including a parent such as `/home`; they are not restricted to the invoking user's projects. Choose roots that describe the intended scope. A scan root that contains the home directory (such as `/home`) disables preselection for all of its candidates. On systems with effective user IDs, a foreign-owned entry protects the entire candidate containing it. Filesystem roots, home itself and linked roots remain rejected; blank CLI scan paths are argument errors.

Each configuration read also compares the descriptor's ctime before and after reading, without comparing it to the pathname's ctime on Windows. Content rechecks retain the bounded read, identity checks and SHA-256 comparison; they do not parse TOML again after the rules have been loaded.

A minimal repository override:

```toml
schema_version = 1

[[rules]]
id = "purge.artifacts"
exclude = ["**/build/keep.json", "**/dist/local/"]

[[rules]]
id = "clean.codex.versions"
keep_previous = 2
```

Rules with an existing `id` inherit omitted fields. A supplied `match` replaces the entire previous expression. New rules default to `auto_apply = false`; builtins declare their own selection defaults. `enabled = false` disables selection, including through references. IDs start with `clean.` or `purge.`; `clean.auto` and `purge.auto` are reserved. `schema_version` remains `1`.

The top-level matching fields are `id`, `match`, `exclude`, `enabled`, and `auto_apply`; retention fields are `min_age_days`, `preselect_after_days`, and `keep_previous`. Optional booleans `recursive` and `include_unverified` configure the selected rule's project scanning, default to `false`, and inherit omitted values on override. `recursive = true` removes the six-level discovery limit; `include_unverified = true` admits artifacts without verified project context or purpose. They do not relax Git, link, mount or frozen-file protections, or widen another selected rule's scope. `match` is required for new rules. Unsupported fields are errors, including `command`, `root`, `source`, `include`, `file_match`, and top-level size/type filters. An empty configuration leaves builtins active.

## Project scan options

For each selected rule, CLI and TOML options combine as follows:

| CLI option | Rule option | Effective behavior |
| --- | --- | --- |
| omitted | omitted / `false` | Depth 6; unverified project context or artifact purpose is excluded. |
| omitted | `true` | Enable that option for this rule. |
| present | either value | Enable that option for all selected project rules. |

Apply the table independently to `--recursive` / `recursive` and `--include-unverified` / `include_unverified`. CLI flags enable behavior; there is no CLI switch to override a configured `true` with `false`. Generic path scans remain recursive, and Agent budgets are independent.

Project rules share directory traversal. Below depth six, only recursive rules can select candidates. Deeper traversal continues only within or toward a recursive rule's anchored entries and PATH limits. Rules sharing an entry share the necessary traversal; a restrictive glob does not imply a separate filesystem traversal budget. Unverified opt-in is also checked per matching rule before binding candidates.

## Cleanup expressions

`match` is a TOML string containing an expression. Combine conditions with `|` (OR), `&` (AND), `!` (NOT), and parentheses; precedence is `! > & > |`. Boolean conditions may omit `=true`: `is_image` means `is_image=true`, and `!is_image` means `is_image=false`. Explicit boolean values remain accepted; query output uses the short forms. Negation applies to a complete condition or parenthesized expression. Spaces and line breaks are allowed between tokens. String operands use single-quoted TOML literals or double-quoted TOML strings with escapes. Operators inside quoted strings are literal characters.

The former condition tables (`all`, `any`, `not`) and `min_file_size` / `max_file_size` fields are no longer accepted. Convert them to expression strings and `file_size` comparisons. Integer sizes in expressions use plain digits: replace TOML separators such as `10_485_761` with `10485761`. Configuration errors stop before scanning; expression errors report the rule ID, configuration path, and line/column within the decoded expression. No automatic configuration migration is performed.

| Condition | Meaning |
| --- | --- |
| `a & b` / `a \| b` | Both conditions / at least one condition must match. Use `(expression)` to group conditions. |
| `!condition` / `!(expression)` | Negate within the applicable file/directory types. |
| `ref = "clean.media"` | Reference a complete rule, including its protections; full ID required. Clean rules may also reference purge rules; purge rules may reference only purge rules. |
| `within = "./cache"` | Match descendants of a directory and provide a scan entry. Relative paths use the declaring TOML's directory; `~` expands to home. |
| `glob = "**/*.tmp"` | Git-style path pattern relative to the branch's entry. Use `!(glob="pattern")` to negate a pattern. |
| `source = "project"` | Purge project discovery; purge also supports `cachedir-tag`. Clean supports `codex.versions`, `claude.versions`, `copilot.versions`, and `codex.staging`. |
| `file_size <= 1MB` | Compare logical bytes using `<`, `<=`, `=`, `!=`, `>=`, or `>`. For a range use `file_size >= 1MB & file_size <= 50MB`. |
| `is_text` / `is_image` / `is_video` | Boolean filename classification; prefix with `!` to select files outside that classification. |
| `is_empty_directory` | Match real empty directories; `!is_empty_directory` selects nonempty directories. |

```toml
[[rules]]
id = "clean.media"
match = 'is_image | is_video'

[[rules]]
id = "clean.cached-media"
exclude = ["keep/**"]
min_age_days = 7

match = '''
  within=".local/cache" & glob="**/*" & ref="clean.media"
'''
```

Use `hgc file clean -r cached-media --dry-run`. CLI `PATH...` intersects the rule's entries; it does not replace `within` or change an anchored glob's base. A rootless generic rule uses CLI PATH as its entry. Purge retains its configured/default scan-root discovery. Empty intersections produce no candidates. The entry directory itself is not selected. Filesystem roots, home itself and linked roots remain protected.

Each `|` branch retains its own entry. A reference with its own entry preserves it; a rootless reference inherits the enclosing entry. Ambiguous conjunctive entries are rejected before scanning. `!` supplies no entry: a negative-only rule needs PATH or a positive enclosing entry. References are resolved after configuration overrides; missing references, purge-to-clean references and cycles are errors. Clean-to-purge references reuse project/cachedir discovery and project safety checks, with the default depth of six. `recursive = true` or `-R` removes that limit; `include_unverified = true` or `--include-unverified` admits unverified project context or artifact purpose. These settings affect project sources; generic paths remain recursive. They require PATH or an anchored entry and do not use automatic home-root discovery. `auto_apply` controls only top-level selection, not referenced rules. Parentheses, negations, and the expanded condition graph are limited to 64 levels; flat chains do not add nesting per operand.

Size/type conditions and their negations match only regular files. Thus `!is_image` traverses directories and selects non-image files. `is_empty_directory` matches only real directories and stops reading as soon as it finds one child. An independent path/source branch may still select a whole directory. All matching rules contribute to the candidate union.

`exclude` remains an ordered Git-style protection list, including ordered `!` exceptions within the list. Selected rules and their reference closure contribute exclusions even if their matching conditions do not select a candidate. Another rule or `|`/`!` cannot undo them. Applicable `min_age_days` and `preselect_after_days` use the largest value, including conditions matching descendants of a whole-directory candidate. `min_age_days` defaults to zero; new generic rules default to seven-day preselection. Agent version sources retain the largest applicable `keep_previous` (default one), plus current/referenced and pending versions. Codex staging always requires strictly more than seven inactive days. Referencing a specialized source preserves its inventory, lock and process protections.

## File size and media-name filters

```toml
[[rules]]
id = "clean.large_non_text"
match = 'file_size >= 100MiB & file_size <= 2GiB & !is_text'
```

The repository provides [`clean.project_default`](../../../hagency-cleanup.toml), applied explicitly with `hgc file clean -r project_default ./ --dry-run`:

- First apply `purge.artifacts` and `purge.cachedirs` to each project directory supplied as CLI `PATH`, **including its `.local`**, with `recursive = true` and `include_unverified = true` configured in the rule. No extra CLI flags are needed. Referenced exclusions and retention constraints still apply.
- Then remove untracked, lowercase `.log` regular files anywhere beneath each supplied project root. Filter the remaining ordinary files under each supplied project's top-level `.local/`: retain text and images up to **1MB inclusive**, and videos up to **50MB inclusive**; other files are candidates. MB means 1,000,000 bytes. Purge may remove a whole artifact/tagged directory containing otherwise retained media or text; the size/type conditions apply to what remains.
- `clean.empty_directories` is an explicit PATH-driven rule, available as `hgc file clean PATH -r empty_directories --dry-run`; `project_default` references it. Scanning selects eligible pre-existing empty directories anywhere below each supplied root, even if they contain no cleanup targets. `--yes` selects all of these candidates; interactive selection can leave them unchecked. After selected candidates are removed, cleanup walks only their ancestor chains and removes directories that have thereby become empty. This cascade has no extra confirmation (and is included in `--yes`), but it never rescans unrelated directories or removes a pre-existing empty directory that was not selected. Each cascaded directory is checked again for configuration, identity, Git, link and mount safety before deletion.
- Planning completes before confirmation or `--yes` deletion. Completed whole purge candidates replace nested file candidates, and the later path scan skips their subtrees. Purge candidates rejected by exclusions or age constraints do not prune the path scan. Overlapping generic path roots reuse directories already visited; an explicit inner root remains inspectable when its outer scan stopped at a protected directory. Selected purge candidates are processed before remaining `.local` candidates, preserving UI order within each phase. Each candidate still undergoes deletion-time checks.
- Supply one or more project roots as `PATH`; for the current project use `./`. This rule has no `within` anchor: its `/.local/**` glob is relative to each supplied root, so loading it from another repository does not change the target project. Omitting PATH is an argument error. Pass the project directory, rather than its `.local` directory. Other rules using `within` still resolve that condition relative to their TOML file. Existing Git, link, mount and Agent protections still apply.

Use `hgc file rule clean.project_default` to inspect the effective expression. This rule remains explicit-only (`auto_apply = false`).

Previews with candidates, including interactive and non-interactive `--dry-run`, display the cascade notice when enabled. Their listed paths and known size cover scan-time candidates; possible ancestor removals are determined after deletion and are not included in those preview totals.

Cascades are enabled by a generic path branch with a positive `is_empty_directory` predicate, such as `ref="clean.empty_directories"`. Negation is tracked through groups and references: `!ref="clean.empty_directories"` does not enable cascades; `!!ref="clean.empty_directories"` does. Project, cachedir and Agent source branches do not enable cascades themselves; project rules may still select existing empty directories during their normal scan. `file purge` never cascades. To opt into ancestor cleanup from a clean rule using project sources, add `| ref="clean.empty_directories"`. The interactive confirmation explains that newly empty ancestors may also be removed. Cascades process deeper directories before their parents, including sibling branches, and authorize only directories proven empty. Each ancestor gets a fresh, bounded verification; a failure is reported while already completed removals remain completed.

Repository policy changes: `clean.project_default` now retains text/images under `.local` only up to **1MB**, previously **10MB**; it also selects untracked lowercase `*.log` files throughout the supplied project roots and enables the empty-directory cascade described above. Inspect `hgc file clean -r project_default ./ --dry-run` before applying this policy. These changes affect this explicit repository rule, not the default automatic rule selection.

Sizes accept nonnegative byte integers or unit values with `B`, `KB/MB/GB/TB` (1000), or `KiB/MiB/GiB/TiB` (1024), case-insensitively. Unit values may be bare or quoted: `1024`, `1MB`, `1.5MiB`, and `"1.5MiB"` are valid. Spaces or tabs may separate the number and unit, so `file_size <= 1 MB` is equivalent to `file_size <= 1MB`. Decimals must resolve to whole bytes. Booleans, bare floats, negative sizes, unknown units, fractional bytes, and values above `2^63-1` are errors. Contradictory comparisons such as `file_size >= 2 & file_size <= 1` match nothing. Filters use logical length (`st_size`), including sparse holes; previews estimate allocated disk space. `hgc file rule` displays a copyable TOML `match` string with canonical byte values, grouping parentheses, and full reference IDs.

Quoted sizes require a unit: use `1024` or `"1024B"`, not `"1024"`.

All name classification is case-insensitive and uses the final extension; it never reads file contents or consults system MIME databases. Lists are centralized in [file_filters.py](../src/hagency_cli/files/cleanup/file_filters.py).

- Text: existing document, log, data, config, source and script extensions, plus names such as `README`, `LICENSE`, `Dockerfile`, `.gitignore`, `.env`, `Cargo.lock`, `poetry.lock`, `uv.lock`, and `.env.*`.
- Image: `.jpg .jpeg .jfif .png .apng .gif .webp .bmp .tif .tiff .ico .avif .heic .heif .jxl .svg`.
- Video: `.mp4 .m4v .mkv .mov .avi .webm .mpg .mpeg .wmv .flv .3gp .3g2 .mts .m2ts .vob .ogv`.

`.svg` is both text and image. `.ts` is text, not video. Unknown names do not match a positive classification. Binary content named `file.txt` is text under this convention; `file.log.1` does not match `.log`. `Cargo.lock`, `poetry.lock`, and `uv.lock` are the only recognized `.lock` names; `yarn.lock`, `Gemfile.lock`, `composer.lock`, and `Dockerfile.dev` are non-text under this fixed list and can be selected by `!is_text`. Add rule exclusions for names you want to retain. Custom classification lists are not supported.

## Large directories and scan progress

Generic `clean` paths and `purge` scanning have no time limit by default. `--scan-timeout SECONDS` accepts a nonnegative integer; `0` means unlimited. A positive budget is shared by all generic paths and candidate processing in the invocation, rather than restarting for each PATH. It does not change Agent-source budgets or the existing deletion revalidation budget. `purge --paths` editing cannot be combined with `--scan-timeout`, including explicit `0`.

```sh
hgc file clean -r project_default ./ --dry-run
hgc file clean -r project_default ./ --scan-timeout 600 --dry-run
```

TTY scans show the stage, current path, inspected-entry count, candidate count and elapsed time on stderr, updating at most five times per second without an estimated percentage. Non-TTY output has no dynamic progress. Ctrl+C during scanning exits with status 130 and never enters deletion.

Python UI adapters may implement `scan_progress(ScanProgress)` to receive throttled progress events, including a final event on completion or interruption. Existing adapters without the optional method continue to work. Final events also contain planning Git-query counts and per-stage timings for diagnostics.

A timeout reports the incomplete scope and the number of fully checked candidates. Those candidates can still be selected and confirmed, or processed automatically with `--yes`; an unfinished candidate or one whose protection checks cannot finish is omitted. Exit status remains 1 even when selected deletions succeed or the user cancels selection. Finalizing already inspected results can take additional time after the budget expires; the budget is checked cooperatively and cannot interrupt a blocking filesystem call. The partial preview is not a complete inventory of the requested paths.

Planning loads tracked paths once per Git repository and reuses ancestor discovery for sibling files and directories. The sorted index stores file paths without duplicating every parent prefix; directory checks use prefix lookup. Memory scales with Git index output and the number of distinct directories cached during ancestor discovery. Rule scopes are reused per project context; a root index limits protection merging to overlapping scopes while preserving policy order. The index retains all scopes for this invocation so that exclusions and descendant age constraints are not evicted. Whole-directory candidates still inspect nested repositories. These caches exist only during planning: deletion rechecks current Git state and the frozen file manifest independently. Candidate deduplication uses a directory-ancestor index instead of comparing every candidate against every previous one. Rule references are expanded once in each loaded snapshot and shared by selection and inspection. Age checks stop evaluating rules that cannot tighten the already established thresholds; complete manifests and exclusion checks remain required.

## Automatic deletion after scanning

`clean` and `purge` accept `--yes` / `-y` to delete all eligible candidates after scanning, without a selection dialog or confirmation. This also enables deletion without a TTY. It includes recent candidates that would normally be unchecked; `preselect_after_days` controls default checkbox selection, not eligibility. Exclusions, `min_age_days`, retained Agent versions, Git/link/mount protections, and deletion-time configuration and frozen-file checks still apply.

```sh
hgc file clean -r project_default ./ --yes
hgc file purge ./project -r artifacts -y
hgc file clean -r project_default ./ --yes --dry-run
```

`--yes --dry-run` previews all candidates without any selection or confirmation and never deletes. Scanning completes before deletion starts; a scan timeout can leave a partial set of fully checked candidates available for automatic deletion, with final exit status 1. `purge --paths` editing cannot be combined with `--yes`. Python callers use `CleanRequest(yes=True)` or `PurgeRequest(yes=True)`.

## Select and inspect rules

```sh
hgc file rule
hgc file rule --command clean
hgc file rule clean.custom-cache
hgc file rule --config ./hagency-cleanup.toml
hgc file clean -r custom-cache --dry-run
hgc file clean ./target -r custom-cache --dry-run
hgc file clean -r auto -r custom-cache --dry-run
hgc file purge ./project -r temp-files -r artifacts -R --dry-run
```

Without `--rule`, clean and purge behave as `--rule auto`: they select enabled rules with `auto_apply = true`. Repeat `--rule` / `-r` to select only the named rules; only the initial `clean.` / `purge.` prefix may be omitted (`clean.codex.staging` becomes `codex.staging`, not `staging`). IDs and `auto` are case-sensitive and matched exactly, without trimming whitespace. `auto` adds that command's automatic rules to the selection. Full IDs and shorthand are deduplicated. Selected rules retain configuration loading order regardless of CLI argument order: builtins first, then new configured rules; overrides retain their original position. Unknown, disabled or cross-command IDs fail before scanning, without falling back to automatic rules. An empty selection prints a `no_rules_selected` notice and scans nothing, returning success even on platforms where a nonempty clean selection is unsupported. `-r` now means rule selection; recursion uses `-R` / `--recursive`.

For `clean`, positional `PATH...` limits selected entries by intersection and supplies roots for unanchored generic/project branches. It does not replace a `within` anchor or change that branch's glob base. Relative CLI paths use the invocation directory, and `~` expands to home. Without CLI paths, selected generic/project branches must have a `within` entry; a missing entry fails the whole invocation before scanning. Unselected rootless rules are valid. A rootless rule may set `auto_apply = true` for invocations that supply PATH; without PATH, selecting it fails with exit status 2. Purge retains its existing scan-root behavior. Rule discovery is independent of targets for both commands.

`hgc file rule` lists all merged rules, including disabled rules, in configuration loading order, with full ID, state (`auto`, `explicit-only`, `disabled`), source and origin. `--command clean|purge` filters the list; a positional **full ID** (no shorthand) prints all effective configuration fields. The ID prefix identifies the derived command; the removed `command` field is not printed. Querying uses the same configuration loading as execution and does not inspect cleanup targets.

`glob` conditions and `exclude` patterns use `GitIgnoreSpec` syntax: `*`, `**`, rooted paths, and directory suffix `/`. Project patterns are relative to the discovered project root, or to the selected scan root for unverified candidates admitted with `--include-unverified`; generic branches use their `within` or invocation root; Agent branches use their source's installation/staging roots. The `exclude` list supports ordered `!` exceptions, with later matches overriding earlier ones. Final exclusion wins across selected rules and their reference closure. Rules outside that closure contribute no candidates or constraints; code-level protections remain enforced. Negating a `glob` condition controls candidate selection; use `exclude` to preserve contents of a selected directory.

Directories are whole candidates: **one excluded descendant preserves the entire directory**. Another rule or an overlapping scan root cannot bypass that exclusion. Overlapping age/retention requirements take the stricter result, including requirements on descendants of a whole-directory candidate. Duplicate paths and nested candidates are coalesced; distinct hard-link paths remain removable while allocated space is counted once. Invalid TOML, duplicate IDs, unsupported fields/sources, malformed patterns and invalid parameter types stop before scanning.

Preview shows exact paths, file/directory type, grouping, allocated size, matched rule IDs, configuration sources and protection reasons as observed during planning. Deletion revalidates that snapshot after confirmation. Control and bidirectional formatting characters are rejected in candidate paths, rule IDs and custom roots, and escaped in error output. By default, a TTY selects entries and confirms permanent deletion; confirmation defaults to No. `--dry-run` keeps selection but skips deletion and destructive confirmation. Non-TTY invocations preview unless `--yes` is supplied. `--yes --dry-run` previews all candidates without either prompt.

After confirmation, the frozen plan is revalidated. File/directory identities, ancestor links, Git state, contents/metadata, Agent references, process occupancy and installer locks are checked again. Changed candidates are skipped and newly discovered targets are never added. `--include-unverified` does not allow a verified candidate to lose its project context after preview; an unverified candidate gaining project evidence also requires a fresh preview because pattern ownership may have changed. Changes to Composer/.NET artifact verification likewise require a fresh preview. If the rules file changes, disappears or appears after an initially missing file, deletion stops and a fresh preview is required. Safety checks are enforced by code and cannot be disabled in TOML. An I/O failure can leave a partly deleted directory; the report identifies failures and any retained `.hagency-cleanup-*` quarantine path. Retained quarantines are protected from subsequent automatic cleanup and require manual inspection. Deletion requires a nonempty frozen manifest including the candidate itself, also on the fallback filesystem path.

Exit status is `0` for success or cancellation, `1` for invalid configuration or incomplete execution, `2` for invalid CLI arguments (including selected rules missing PATH/root), and `130` for interruption. Python callers receive `CleanupSelectionError` for invalid invocations; configuration and execution failures are returned in `CleanupReport`.
