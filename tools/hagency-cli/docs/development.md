# Development

[CLI guide](../README.md) | [Local model proxy](model-proxy.md) | [Cleanup rules](cleanup-rules.md)

The CLI is organized around workspace content, project files, and the model proxy. It uses domain concepts to assign ownership, with operation functions composing the work. It does not require repositories, an event bus, or a dependency injection container.

| Package | Ownership |
| --- | --- |
| `cli.py` | Command tree, entrypoint, narrowly scoped legacy argument normalization |
| `commands` | Typer options, completion, terminal output, Questionary adapters, exit codes |
| `workspace/discovery.py` | Hagency root discovery and initialization |
| `workspace/config.py` | Hagency TOML encoding and persistence |
| `workspace/sources.py` | Source definitions, checkout resolution, Git synchronization |
| `workspace/skills.py` | Skill discovery, selectors, conflict resolution, symlink/copy/junction installation |
| `workspace/profiles.py` | Profile definitions, selection, application, source reference checks |
| `workspace/source_inputs.py` | Read-only classification and source reuse decisions for shortcut installation |
| `workspace/catalog.py` | Discoverable skill queries shared by commands and completion |
| `workspace/operations` | Source, profile, and skill workflows |
| `files/sync` | Configuration, selection/scanning, planning, content comparison, SFTP and offline bundles |
| `files/purge` | Purge scan-root discovery and the saved paths editor |
| `files/cleanup` | Rule loading/matching, project and Agent inspection, frozen plans, statistics, revalidation and deletion |
| `model_proxy` | Existing protocol, provider, hook, daemon and worker contracts |
| `paths.py` | Shared path expansion and platform normalization |

Dependencies flow from command adapters to operations and business modules. Profiles use skill discovery and installation; skills use source definitions. The source module does not read profiles: the source removal operation asks the profile module for references before removing a source. File sync and cleanup remain independent. Cleanup reuses Hagency workspace location but does not parse its source/profile schema.

Workspace operations return results or reports and raise `WorkspaceError` subclasses. They emit optional `OperationEvent` callbacks rather than printing. Command adapters render progress and contextual retry commands. File operations retain their reports, error types, and progress callbacks. `RemoteFileSystem`, `CleanupUI`, and `SkillConflictUI` remain focused interfaces; shortcut multiselect extends the skill interaction contract with `SkillSelectionUI`.

Questionary imports happen when interactive adapters are used. SFTP imports Paramiko when connecting. The CLI can load lightweight proxy daemon/configuration code; ordinary commands and help do not import the HTTP server. The worker remains `hagency_cli.model_proxy.worker`, with unchanged hook contracts and service state locations. Python consumers import the owning modules directly. Importing `hagency_cli.model_proxy.server` loads the HTTP implementation. Hook-facing types such as `AuthPatch` and `HeaderPatch` are exported from `hagency_cli.model_proxy`.

## Maintaining help

Command docstrings hold the full operational help; `short_help` keeps command lists concise. Aliases reuse those docstrings. The command group's formatter combines the registered aliases into one row while preserving command parsing and completion. A registration check verifies that its display map matches shared callbacks and Typer instances, including within each command group. Options use Typer's native multiple name declarations. Example blocks use Click's `\b` paragraph marker to preserve newlines. Help is part of the package and never loads Markdown files at runtime.

When changing a command, update its help and the matching [user guide](../README.md#documentation) together. Keep runtime behavior in the owning operation module, and keep help usable from an empty directory without loading interactive, SFTP, or HTTP implementations.

## Filesystem and transport implementation

Explicit SFTP agents belong to individual connections; connecting does not modify `SSH_AUTH_SOCK`. Unix agent sockets use the connection timeout. Native Windows retains Paramiko's Pageant/OpenSSH discovery. The adapter's small integration with Paramiko's existing agent authentication is tested with Paramiko 3.5.1 and 4.0.0, including signing through a temporary local OpenSSH agent. Online sync preserves symlink targets, including absolute and dangling links, without scanning through them. Offline bundles continue to skip symlinks.

Cleanup's descriptor-based directory removal claims a unique temporary name for the emptied artifact, checks its identity while retaining the open descriptor, and removes that name. This preserves directories concurrently recreated at the original path. If the final identity check or removal fails, the report identifies the quarantine path where remaining contents are retained.

## Validation

From the [`tools/hagency-cli` directory](../):

```sh
uv run --frozen python -m unittest discover -s tests
uv build --out-dir /tmp/hagency-cli-dist
git diff --check
```

The suite includes command and alias help, CLI `main()` parsing and command removal, dependency and lazy-import boundaries, workspace discovery, local Git acquisition/update behavior, selection/cancellation, dry runs, installation modes, SFTP credential/Git protection, bundle validation before writes, cleanup configuration discovery, rule matching, Agent version retention/occupancy, frozen-plan revalidation, and proxy lifecycle coverage. Mocks target the module that owns each dependency.

Release validation also installs the wheel in an isolated environment outside a Hagency workspace and tests an isolated editable installation. Native filesystem behavior is established only for the host platform; mocked Windows/macOS branches do not establish native execution, and local/fake transports do not establish live SFTP or upstream provider behavior.
