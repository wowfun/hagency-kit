# SFTP file sync

[CLI guide](../README.md) | [Offline bundles](sync-bundles.md)

Initialize the reference VSCode-SFTP template in an existing project directory, then edit its placeholder connection values. Initialization does not connect to a server or include a password or private key. It refuses to replace an existing config unless `--force` is supplied; `--dry-run` prints the planned path and template without creating files:

```sh
hgc file init --root /path/to/project
hgc file init --root /path/to/project --dry-run
hgc file init --root /path/to/project --force
```

Run file sync from a project directory containing `.vscode/sftp.json`. The command uses the same `context` to `remotePath` mapping as VSCode-SFTP:

```sh
hgc file push --dry-run
hgc file push
hgc file push --git-changed --dry-run
hgc file pull
hgc file sync
```

`push` uploads, `pull` downloads, and `sync` synchronizes in both directions.

For one-off directory-tree sync, pass an SCP-style endpoint. This mode uses the current directory as the local root unless `--root` is given, and it completely bypasses `.vscode/sftp.json` even when that file is present or malformed:

```sh
hgc file push dev@server:/srv/project --dry-run
hgc file pull server:~/Projects/ws --root ./restore
hgc file sync server:C:/Projects/ws --exclude '*.tmp'
hgc file push '[2001:db8::1]:/srv/project' -P 2222 -i ~/.ssh/id_ed25519
```

The endpoint must contain a non-empty remote path. Use `host:.` for the remote home directory; `host:~/path`, SSH aliases, explicit `user@host`, bracketed IPv6, and Windows drive paths are supported. All temporary directions accept `-P/--port`, `-i/--identity`, repeatable `--exclude`, `--skip-create`, and `--ignore-existing`. The two one-way directions also accept `--delete` and `--update`, while `push` additionally accepts `--git-changed`; `sync` intentionally does not expose `--delete` or `--update`. These temporary-only options require an endpoint, and endpoint mode is mutually exclusive with `--profile`.

Temporary sync has safe non-deleting defaults and always excludes `.git` files/directories and every `.vscode/sftp.json`, including nested projects; later user negation rules cannot re-include them. It has no password option. Authentication uses the SSH agent, default keys, SSH config, or `--identity`; load encrypted private keys into the agent first. The endpoint user, `-P`, and `-i` take precedence over SSH config, while `HostName`, `User`, `Port`, `IdentityFile`, and `ProxyCommand` are read from the default `~/.ssh/config`. Port 22 and the current system user are the final fallbacks. Existing host-key verification still applies.

`push` and `pull` treat the named side as the source and make it win shared-path conflicts. By default they copy missing files and use whole-second modification time plus size to identify possible overwrites, while destination-only paths remain untouched. Before overwriting a shared regular file, the command first skips content comparison if the sizes prove that CRLF normalization cannot make the files equal. Otherwise it reads both copies and skips the action when their bytes match or their text differs only by CRLF versus LF line endings. Files containing a NUL byte are compared as binary data, a lone CR remains significant, and transferred bytes are never rewritten. This extra content read also applies to dry runs. `syncOption.update`, `ignoreExisting`, `skipCreate`, and `delete` retain their VSCode-SFTP meanings; `delete = true` removes destination-only paths, so preview that plan first. `sync` copies unique files to the other side and makes the precisely newer version of a shared file win, with local winning an exact timestamp tie; as in VSCode-SFTP, only `skipCreate` and `ignoreExisting` affect this mode.

**Detection limit:** same-size edits within the same modification-time second can be missed, including with `--git-changed`; that flag selects paths and does not force content comparison. There is currently no checksum/force option. Completion counts describe planned actions, not the number of files changed; deleting an already-absent path or a nonempty protected directory may leave the filesystem unchanged.

Use `hgc file push --git-changed` to restrict an upload to staged, unstaged, untracked, deleted, and renamed paths reported for the configured local `context`, or the temporary mode's local root. Git-ignored paths are excluded. A rename uploads the new path; removing the old remote path, like any deletion, still requires `syncOption.delete = true` in config mode or `--delete` in temporary mode. The Git path set only narrows the normal sync plan, so ignore and sync options remain authoritative. Scanning keeps changed paths and their parents and skips unrelated subdirectories before planning; each visited directory is still enumerated. If no Git changes exist, the command returns without connecting to SFTP. Git is never inspected during ordinary sync, so Git need not be installed and the project need not be a repository; when `--git-changed` is explicitly requested, missing Git, a non-repository context, or a Git inspection failure is reported before any remote connection. Each Git inspection command has a 120-second timeout.

Online sync uses one SFTP connection and applies transfers sequentially. The implementation supports SFTP configs, `context`, Windows-style remote paths, `ignore`, `ignoreFile`, `remoteTimeOffsetInHours`, `filePerm`, `dirPerm`, `useTempFile`/`openSsh`, password, private-key, SSH-agent, and `~/.ssh/config` authentication. Configured permissions are applied when remote files or directories are created or uploaded; permissions on existing remote directories are not reconciled. All online sync modes protect `.git` files/directories and `.vscode/sftp.json` at every depth on both sides, including case variants; user negations cannot re-include them. Worktree and submodule Git metadata files are not synchronized. SSH host keys must already be trusted in the user's known-hosts files. A single config is selected automatically. For arrays or nested `profiles`, use `--profile NAME`; ambiguous short names are rejected, `--profile CONFIG:PROFILE` selects a nested profile, and `--profile CONFIG:` selects the base config without its `defaultProfile`. Otherwise, `defaultProfile` is selected automatically. Use `--root <directory>` to target a config outside the current directory. Except for a clean `--git-changed` upload, `--dry-run` still connects and scans the remote side, but performs no file changes.

## Remote WSL over SFTP

A Windows OpenSSH alias whose `RemoteCommand` starts WSL is still a Windows SFTP endpoint: OpenSSH configures SFTP as a separate subsystem, and `hgc` does not interpret `RemoteCommand` or `RequestTTY`. To sync a WSL filesystem remotely, run a standard sshd inside the target WSL distribution and expose it through a reachable address or Windows port forwarding/firewall rule, then give that endpoint its own alias ([Microsoft OpenSSH Server configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)):

```sshconfig
Host win-wsl
  HostName windows-host
  User <wsl-linux-user>
  Port 2222
```

```sh
hgc file push win-wsl:/home/<wsl-linux-user>/Projects/ws --dry-run
hgc file pull win-wsl:~/Projects/ws -r ./restore
```

Do not add `RemoteCommand` or `RequestTTY` to this alias. `hgc` does not install WSL sshd, configure Windows networking, or edit SSH config. On native Windows, a path such as `\\wsl.localhost\DISTRO\home\...` can be used as the local `--root`, but it is not the remote WSL transport described above, and cross-filesystem access may have a performance cost ([Microsoft WSL filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems)).
