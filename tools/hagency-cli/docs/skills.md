# Skills

[CLI guide](../README.md) | [Sources](sources.md) | [Profiles](profiles.md)

`skill list` discovers `SKILL.md` directories in the workspace and registered source content. It does not track installed skills or fetch missing checkouts:

```sh
hgc skill list
hgc skill list --source workspace
hgc skill list --source repo --root ./kit
hgc skill list --profile dev
```

Repeat `--source` to narrow discovery; it can be combined with `--profile`. Output columns are source, name, selector, and path. Run `source sync` before discovery when remote content is missing or needs updating. Installation uses the [destination rules](configuration.md#installation-destinations).

```sh
hgc skill add owner/repo --skill code-review --skill tools/testing
hgc skill add https://github.com/owner/repo.git --ref main --all
hgc skill add ./local-skills --all
hgc skill add existing-source --skill nested/one --dry-run
```

A resolvable Hagency workspace is required; installation never initializes one implicitly. Inputs accept a discovered skill name, exact `SOURCE:selector`, registered source name, Git URL, GitHub `owner/repo`, or explicit local directory (`./`, `../`, `~`, absolute and Windows paths). Registered sources and their selectors take precedence over GitHub shorthand. Local paths resolve from the invocation directory.

Repeat `--skill/-s` for source-relative selectors, or use `--all`; these options are mutually exclusive and cannot be added to an exact skill reference. Selectors must stay within the source: absolute paths, escaping `..` paths, and symlinks pointing outside are rejected. A source with one skill installs directly. Multiple skills without filters require terminal multiselect with nothing selected by default; non-interactive use requires selectors or `--all`. Cancellation or an empty selection installs nothing. All selections and duplicate-name conflicts are resolved before the first installation write, including with `--all`.

Remote inputs reuse the unique source matching the URL and any explicit ref. GitHub URLs normalize trailing `/` and `.git` within the same transport; other URLs match exactly, and HTTPS is not equated with SSH. Multiple matches require `--source-name`. A new remote source uses the repository name, then `owner/repo` on a name collision, then requires an explicit name. A local directory reuses the most specific containing source, falling back to the workspace, and limits discovery to the supplied subtree. Tied sources require a name; otherwise a new local source uses the directory name. New names cannot be `workspace` or contain path traversal or ambiguous references.

New remote sources use workspace default ref/depth. Only missing checkouts are fetched; existing URL/ref/branch settings and checkout contents are preserved. An explicit ref mismatch requires `source sync` first. An occupied, unregistered checkout path fails before registration. `--checkout-dir` is saved as the resolved checkout path for a new remote source; for existing sources it affects only the invocation's checkout discovery. After static validation, installation registers, obtains, selects, then installs. A fetch failure or cancellation retains the source registration and any checkout, with progress describing completed stages; installation does not promise rollback.

Skill installation `--dry-run` performs no network access or config/target writes. If the checkout is missing, it reports proposed registration/fetch steps and marks candidates and the installation plan as unverified. Existing checkouts use read-only Git commands to verify an explicit ref; a local subdirectory inside a remote checkout also accepts `--ref`. `skill list/ls` lists discoverable skills; it is not an installed-skill inventory.

Installation uses symlinks on non-Windows systems and junctions on Windows. Independent directories are not overwritten; existing links of the selected type may be retargeted. Unselected installations remain in place. Installation has no tracking or pruning and is not transactional.
