# Offline sync bundles

[CLI guide](../README.md) | [SFTP file sync](file-sync.md)

Use `pack` and `apply` when SSH, SFTP, or network access is unavailable. `pack` reads local files only and creates a portable ZIP; transfer that ZIP through any external channel, then verify and apply it locally at the destination:

```sh
hgc file pack --root /path/to/source
hgc file pack -r /path/to/source -o release.zip --force
hgc file pack -r /path/to/source --git-changed --exclude '*.tmp'
hgc file apply release.zip --root /path/to/destination --dry-run
hgc file apply release.zip -r /path/to/destination --delete
```

The default output is `hgc-sync.zip` in the invocation directory. Existing output is refused unless `--force` is explicit; output uses a same-directory temporary file and atomic replacement. `--dry-run` reads and hashes the selected source files but creates no archive. The source defaults to the current directory. When `.vscode/sftp.json` exists, `pack` reuses only profile selection, `context`, `ignore`, and `ignoreFile`; it neither validates nor records connection or authentication fields and never opens an SFTP connection. A missing config means ordinary-directory mode. Invalid JSON or ambiguous profile selection is an error; `--no-config` bypasses config discovery completely and is mutually exclusive with `--profile`.

A normal pack is a full source snapshot. `--git-changed` instead creates a Git patch containing staged, unstaged, untracked, deleted, and renamed paths; old rename paths become deletion markers. Packing and applying a Git patch skip unrelated subdirectories during scanning. A clean or fully filtered worktree creates no bundle. Git is not needed for full packs or ordinary directories. Git metadata (`.git` files/directories), all nested `.vscode/sftp.json` configs, the output bundle itself, config ignore rules, and repeatable `--exclude` patterns remain excluded. Symlinks are never followed or packed: they are listed in the manifest and printed as warnings while pack still succeeds.

The ZIP contains a versioned `manifest.json` and file bytes under `payload/`. The uncompressed manifest is limited to 16 MiB; oversized manifests are rejected before decompression or destination writes. Packing, including dry runs, enforces the same limit. This limits the manifest only; total uncompressed payload size is not capped. The manifest records portable relative paths, type, size, nanosecond mtime, POSIX mode, SHA-256, effective ignore rules, deletion markers, and skipped symlinks, but no host, credentials, or absolute source path. SHA-256 detects corruption; the bundle is not signed, authenticated, or encrypted.

Before reading payloads, bundle verification rejects entries and deletion markers that target Git metadata, SFTP configs at any depth, or paths excluded by the manifest. Negation rules cannot override metadata protection, including case variants. Before any destination write, `apply` validates the complete archive, manifest version, entry set, safe paths, duplicate and cross-platform path collisions, sizes, and hashes. The destination directory may be absent and is created only during a real apply. The bundle wins shared-path conflicts by default. `--skip-create`, `--ignore-existing`, and `--update` retain the one-way sync meanings. For a full bundle, `--delete` removes destination-only, non-ignored paths; for a Git patch it applies only explicit deletion markers and never expands into a destination mirror. Writes are atomic and deletions run last. Text differing only by CRLF versus LF is left untouched, NUL-containing files use raw-byte comparison, and transferred bytes are never rewritten. New POSIX files restore source mode, overwritten files preserve destination mode, Windows does not force POSIX permissions, and mtime is restored where supported.
