# Sources

[CLI guide](../README.md) | [Configuration](configuration.md) | [Skills](skills.md) | [Profiles](profiles.md)

Register a directory or Git repository, inspect it, then sync its content:

```sh
hgc source add local-skills --path ./skills --dry-run
hgc source add https://github.com/owner/repo.git --ref main --sync
hgc source list
hgc source show repo
hgc source sync --profile dev --depth 1 --dry-run
hgc source sync --profile dev --depth 1
hgc source sync --profile dev --slice 1,3:
```

`source add` requires an existing workspace. Pass a name with `--path` for a local source, a name with `--url` for a remote source, or a Git URL to infer the name. If that name is occupied, URL inference tries `owner/repo`; `--name` overrides inference. `--ref` and `--remote-name` require a remote URL. `--sync` writes the registry first and then synchronizes; a sync failure retains the registration. `--dry-run` previews both stages without fetching or writing.

`source list` prints name, type, resolved path, URL, and ref. `source show NAME` prints one source's configured and resolved values. Both read local configuration without fetching missing checkouts.

With no names or `--profile`, sync selects all configured sources; explicit names and profile sources combine. Local source paths are checked without fetching. `--slice/-s` applies after selection using 1-based indexes and inclusive ranges such as `4:`, `2:4`, `:3`, or `1,3:`. Failures are reported per source while other selected sources continue. Retry failed names or slices; no resume state is saved. Transient Git network failures are retried automatically, with a five-minute deadline per Git command.

Normal sync refuses non-fast-forward updates. If upstream rewrites history and a checkout is disposable, `--reanchor` can replace local-only commits with fetched upstream history. It requires no staged, unstaged, or untracked changes and creates no recovery refs:

```sh
hgc source sync repo --reanchor --dry-run
hgc source sync repo --reanchor
```

Sync dry runs do not fetch or change files, so they describe reanchoring conditionally. Use reanchoring only when discarding local-only commits is intended.

`source remove NAME` removes only the registry entry, leaving the checkout and installed skills in place. Profile references block removal unless `--force` is supplied; forced removal leaves those references unchanged. Preview with `--dry-run`, and update profiles separately when removing a referenced source.
