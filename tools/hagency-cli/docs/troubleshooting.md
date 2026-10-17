# Troubleshooting

[CLI guide](../README.md)

| Symptom | Next step |
| --- | --- |
| `not a hagency workspace` | Run `hgc init` for a new workspace, or pass `--root` for an existing one. Wheel installs have no editable-checkout fallback. |
| Skills missing from discovery | Check `source list` paths and run `source sync` for the relevant sources; discovery never fetches content. |
| Ambiguous skill/source selection | Use `SOURCE:selector`, `--source-name`, or narrower `--skill`/profile include selectors. Non-TTY installation needs explicit selection when multiple skills exist. |
| Destination differs from expectation | `--path` is the final container; `--dir` appends `.agents/skills`. Both resolve from the invocation directory independently of `--root`. |
| Existing destination refused | Inspect the destination; independent directories are protected. Choose another destination for a separate copy. |
| Sync cannot fast-forward | Inspect local commits. Use `--reanchor` only for a disposable, clean checkout whose local-only commits can be discarded. |
| SFTP profile selection is ambiguous | Use `--profile CONFIG:PROFILE` or `CONFIG:` for the base config. Temporary endpoints cannot accompany `--profile`. |
| Online sync misses a same-size edit | The mtime/size filter can miss edits within one second. `--git-changed` narrows paths but does not force comparison. |
| SFTP host key or authentication fails | Establish trust in known-hosts and check SSH config/agent credentials. Preload encrypted keys; a Windows `RemoteCommand wsl` alias does not provide WSL SFTP. |
| Purge previews without deleting | By default, non-TTY runs preview and TTY runs require selection and confirmation. Use `--yes/-y` to delete all eligible candidates automatically after scanning. `--dry-run` always prevents deletion. |
| Proxy fails to start or reload | Check the printed log, config, environment, hooks, and port. Use the same config path and repeat custom listener options when restarting. |

## Dry runs and failures

Dry-run semantics depend on the operation:

| Command family | Preview behavior |
| --- | --- |
| Workspace/source/profile config edits | Print proposed changes without writing; source previews do not fetch. |
| `skill add` / `profile apply` | Preview without target writes or network access; conflicts never prompt. Missing checkouts can make a skill-install preview provisional. |
| Online `file push/pull/sync` | Connect and read both sides, including content comparisons, without writes/deletes. A clean Git-filtered upload skips connection. |
| `file pack/apply` | Read, hash, or verify local data without creating the ZIP or changing the destination. |
| `file purge`, `file clean` | Previews only. TTY selection opens unless `--yes` is supplied; `--yes --dry-run` previews all candidates without prompts. |

The model-proxy lifecycle and `file purge --paths` have no dry-run mode. Invalid CLI syntax or option combinations generally exit with status 2; operation failures generally use status 1. A failed batch may have completed earlier work: source sync continues other sources, skill acquisition retains registration/checkouts, and purge or file transfers can leave partial results. Inspect the progress report before retrying.
