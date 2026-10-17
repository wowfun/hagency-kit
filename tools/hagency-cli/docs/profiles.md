# Profiles

[CLI guide](../README.md) | [Configuration](configuration.md) | [Sources](sources.md) | [Skills](skills.md)

Create or edit reusable skill selections, then apply them to a target container:

```sh
hgc profile add review --description "Review changes" -AS workspace:skills/analyze-diff
hgc profile update review -AS workspace -i skills/log-analyzer --dry-run
hgc profile update review -AS workspace -i skills/log-analyzer
hgc profile show review
hgc profile list
hgc source sync --profile review
hgc profile apply review --dir ./project --dry-run
hgc profile apply review --dir ./project
```

`profile add` creates a new profile; without `--add-skill/-AS`, it is empty. `profile update` edits an existing one. `-AS` accepts a source, unique discovered skill name, or `SOURCE:selector`. It merges the selected entry by default; `--replace` rewrites that entry's selection. `--include/-i` and `--exclude/-e` are repeatable and require `-AS`; `--replace` also requires it. Exact references supply an include selector automatically. Ambiguous skill names require an explicit source reference. Missing source content must be obtained with `source sync` before selector validation.

`profile update -RS/--remove-skill` removes a source entry or exact included selector and cannot accompany `-AS`. A description-only update is allowed. `profile show` prints saved TOML; `profile list` prints names, descriptions, and selected source names. To list individual skill candidates use `skill list --profile NAME`.

`profile remove NAME` deletes the profile directory and its contents; installed skills and source checkouts remain in place. Add, update, and remove support `--dry-run`. Editing configuration does not apply it to existing installation targets.

`profile apply` requires exactly one destination: `--path` for the exact skills container or `--dir` for a project's `.agents/skills`. It requires locally available source content and never syncs implicitly:

```sh
hgc profile apply review --path ./custom/skills --root ./kit
hgc profile apply review --dir ./project -cp
hgc profile apply review --dir ./project --link-mode junction
```

The default materialization mode is symlink, or junction on Windows. `-cp` and `--link-mode copy` create independent copies; `-cp` conflicts with explicit symlink or junction modes. Junction mode is for Windows; explicit Windows symlinks may require an elevated terminal. Applying a profile refuses to overwrite independent directories, can retarget existing links of the selected type, and leaves unselected installations in place. There is no tracking or pruning; installation is not transactional.

If multiple skill directories share an installation name, a TTY prompts for the source path for this invocation without changing the profile. Non-TTY application fails with guidance to narrow its include selectors. `--dry-run` prints the plan without writes and lists all conflicting candidates without prompting, even in a TTY.
