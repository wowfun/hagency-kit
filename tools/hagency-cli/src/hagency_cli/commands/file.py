from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.cleanup_render import (
    render_cleanup_report,
    render_cleanup_rules,
    render_paths_edit_report,
)
from hagency_cli.commands.completion import complete_directory
from hagency_cli.commands.shared import command_errors, diagnostic, die, make_app
from hagency_cli.files.cleanup.agents import AGENTS
from hagency_cli.files.cleanup.config import CleanupConfigError, load_rules
from hagency_cli.files.cleanup.models import (
    CLEANUP_COMMANDS,
    CleanRequest,
    PurgeRequest,
)
from hagency_cli.files.cleanup.operations import clean_files, purge_space
from hagency_cli.files.cleanup.selection import CleanupSelectionError
from hagency_cli.files.purge.roots import edit_purge_paths
from hagency_cli.files.sync.bundle.apply import apply_sync_bundle
from hagency_cli.files.sync.bundle.pack import pack_sync_bundle
from hagency_cli.files.sync.config import initialize_sftp_config
from hagency_cli.files.sync.models import (
    FileSyncConfigError,
    FileSyncError,
    FileSyncUsageError,
    SyncDirection,
)
from hagency_cli.files.sync.operations import sync_workspace_files
from hagency_cli.paths import expand_path

file_app = make_app(
    help_text="Transfer project files and clean up local artifacts.",
    add_completion=False,
)

SFTPProjectRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help="Project/config root, or local root for a temporary endpoint",
        autocompletion=complete_directory,
    ),
]


SFTPRemoteArgument = Annotated[
    str | None,
    typer.Argument(
        help="Use [user@]host:path in place of .vscode/sftp.json and --profile",
        metavar="REMOTE",
    ),
]


SFTPProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        "-p",
        help=(
            "Select NAME, CONFIG: (base config), or CONFIG:PROFILE"
        ),
    ),
]


SFTPSyncDryRunOption = Annotated[
    bool,
    typer.Option(
        "--dry-run", help="Connect and compare both sides without writing or deleting files"
    ),
]


SFTPGitChangedOption = Annotated[
    bool,
    typer.Option(
        "--git-changed",
        help="Only upload paths changed in the local Git working tree",
    ),
]


SFTPPortOption = Annotated[
    int | None,
    typer.Option(
        "--port",
        "-P",
        min=1,
        max=65535,
        help="SSH port (temporary endpoint only)",
    ),
]


SFTPIdentityOption = Annotated[
    str | None,
    typer.Option(
        "--identity",
        "-i",
        help="SSH private key file (temporary endpoint only)",
    ),
]


SFTPExcludeOption = Annotated[
    list[str] | None,
    typer.Option(
        "--exclude",
        help="Gitignore-style pattern; repeatable (temporary endpoint only)",
    ),
]


SFTPSkipCreateOption = Annotated[
    bool,
    typer.Option(
        "--skip-create",
        help="Do not copy source-only paths (temporary endpoint only)",
    ),
]


SFTPIgnoreExistingOption = Annotated[
    bool,
    typer.Option(
        "--ignore-existing",
        help="Do not replace existing paths (temporary endpoint only)",
    ),
]


SFTPDeleteOption = Annotated[
    bool,
    typer.Option(
        "--delete",
        help="Delete destination-only paths (temporary endpoint only)",
    ),
]


SFTPUpdateOption = Annotated[
    bool,
    typer.Option(
        "--update",
        "-u",
        help="Do not replace newer destination paths (temporary endpoint only)",
    ),
]


SyncBundlePackRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help="Source project root",
        autocompletion=complete_directory,
    ),
]


SyncBundleApplyRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help="Destination root",
        autocompletion=complete_directory,
    ),
]


SyncBundleExcludeOption = Annotated[
    list[str] | None,
    typer.Option(
        "--exclude",
        help="Additional Gitignore-style source pattern; repeatable",
    ),
]


SyncBundleDryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Build and print the plan without writing files"),
]


SyncBundleDeleteOption = Annotated[
    bool,
    typer.Option("--delete", help="Apply authorized destination deletions"),
]


SyncBundleSkipCreateOption = Annotated[
    bool,
    typer.Option("--skip-create", help="Do not create paths missing at destination"),
]


SyncBundleIgnoreExistingOption = Annotated[
    bool,
    typer.Option("--ignore-existing", help="Do not replace existing paths"),
]


SyncBundleUpdateOption = Annotated[
    bool,
    typer.Option("--update", "-u", help="Do not replace newer destination paths"),
]


SyncBundleGitChangedOption = Annotated[
    bool,
    typer.Option(
        "--git-changed",
        help="Only pack paths changed in the local Git working tree",
    ),
]


def sync_files_command(
    *,
    direction: SyncDirection,
    root_value: str | None,
    profile: str | None,
    remote_endpoint: str | None,
    port: int | None,
    identity: str | None,
    exclude: list[str] | None,
    delete: bool,
    skip_create: bool,
    ignore_existing: bool,
    update: bool,
    git_changed: bool,
    dry_run: bool,
) -> None:
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    try:
        report = sync_workspace_files(
            root,
            direction,
            profile=profile,
            git_changed=git_changed,
            dry_run=dry_run,
            progress=print,
            remote_endpoint=remote_endpoint,
            port=port,
            identity=expand_path(identity, Path.cwd()) if identity else None,
            exclude=exclude or (),
            delete=delete,
            skip_create=skip_create,
            ignore_existing=ignore_existing,
            update=update,
        )
    except FileSyncUsageError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))

    action_count = len(report.actions)
    if action_count == 0:
        print("already in sync")
    elif dry_run:
        print(f"sync plan: {action_count} action(s)")
    else:
        print(f"sync complete: {action_count} planned action(s)")


def init_sftp_config_command(
    *, root_value: str | None, force: bool, dry_run: bool
) -> None:
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    try:
        initialize_sftp_config(
            root,
            force=force,
            dry_run=dry_run,
            progress=print,
        )
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))


def pack_sync_bundle_command(
    *,
    root_value: str | None,
    profile: str | None,
    no_config: bool,
    output: str | None,
    force: bool,
    git_changed: bool,
    exclude: list[str] | None,
    dry_run: bool,
) -> None:
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    output_path = expand_path(output, Path.cwd()) if output else None
    try:
        pack_sync_bundle(
            root,
            profile=profile,
            no_config=no_config,
            output_path=output_path,
            force=force,
            git_changed=git_changed,
            exclude=exclude or (),
            dry_run=dry_run,
            progress=print,
            warning=lambda message: diagnostic(message, "warning"),
        )
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))


def apply_sync_bundle_command(
    *,
    bundle_value: str,
    root_value: str | None,
    delete: bool,
    skip_create: bool,
    ignore_existing: bool,
    update: bool,
    dry_run: bool,
) -> None:
    bundle_path = expand_path(bundle_value, Path.cwd())
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    try:
        apply_sync_bundle(
            bundle_path,
            root,
            delete=delete,
            skip_create=skip_create,
            ignore_existing=ignore_existing,
            update=update,
            dry_run=dry_run,
            progress=print,
        )
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))


@file_app.command("push", short_help="Upload project files over SFTP.")
@command_errors
def local_to_remote_sync_cli(
    remote_endpoint: SFTPRemoteArgument = None,
    root: SFTPProjectRootOption = None,
    profile: SFTPProfileOption = None,
    port: SFTPPortOption = None,
    identity: SFTPIdentityOption = None,
    exclude: SFTPExcludeOption = None,
    delete: SFTPDeleteOption = False,
    skip_create: SFTPSkipCreateOption = False,
    ignore_existing: SFTPIgnoreExistingOption = False,
    update: SFTPUpdateOption = False,
    git_changed: SFTPGitChangedOption = False,
    dry_run: SFTPSyncDryRunOption = False,
) -> None:
    """Upload project files over SFTP. Local files win conflicts. Destination-only
    files are kept unless deletion is enabled in the config or with --delete.

    \b
      hgc file push dev@server:/srv/project -r ./project --dry-run
    """
    sync_files_command(
        direction=SyncDirection.LOCAL_TO_REMOTE,
        remote_endpoint=remote_endpoint,
        root_value=root,
        profile=profile,
        port=port,
        identity=identity,
        exclude=exclude,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        git_changed=git_changed,
        dry_run=dry_run,
    )


@file_app.command("pull", short_help="Download project files over SFTP.")
@command_errors
def remote_to_local_sync_cli(
    remote_endpoint: SFTPRemoteArgument = None,
    root: SFTPProjectRootOption = None,
    profile: SFTPProfileOption = None,
    port: SFTPPortOption = None,
    identity: SFTPIdentityOption = None,
    exclude: SFTPExcludeOption = None,
    delete: SFTPDeleteOption = False,
    skip_create: SFTPSkipCreateOption = False,
    ignore_existing: SFTPIgnoreExistingOption = False,
    update: SFTPUpdateOption = False,
    dry_run: SFTPSyncDryRunOption = False,
) -> None:
    """Download project files over SFTP. Remote files win conflicts. Local-only
    paths are kept unless deletion is enabled in the config or with --delete.
    """
    sync_files_command(
        direction=SyncDirection.REMOTE_TO_LOCAL,
        remote_endpoint=remote_endpoint,
        root_value=root,
        profile=profile,
        port=port,
        identity=identity,
        exclude=exclude,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        git_changed=False,
        dry_run=dry_run,
    )


@file_app.command(
    "sync", short_help="Synchronize local and remote project files over SFTP."
)
@command_errors
def bidirectional_sync_cli(
    remote_endpoint: SFTPRemoteArgument = None,
    root: SFTPProjectRootOption = None,
    profile: SFTPProfileOption = None,
    port: SFTPPortOption = None,
    identity: SFTPIdentityOption = None,
    exclude: SFTPExcludeOption = None,
    skip_create: SFTPSkipCreateOption = False,
    ignore_existing: SFTPIgnoreExistingOption = False,
    dry_run: SFTPSyncDryRunOption = False,
) -> None:
    """Copy files both ways over SFTP without deleting them. A file present on
    one side is copied to the other. For shared files, the newer version wins
    and an exact timestamp tie favors the local file.
    """
    sync_files_command(
        direction=SyncDirection.BOTH,
        remote_endpoint=remote_endpoint,
        root_value=root,
        profile=profile,
        port=port,
        identity=identity,
        exclude=exclude,
        delete=False,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=False,
        git_changed=False,
        dry_run=dry_run,
    )


@file_app.command(
    "init", short_help="Initialize .vscode/sftp.json in a project directory."
)
@command_errors
def init_sftp_config_cli(
    root: SFTPProjectRootOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing SFTP config")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the config without changing files"),
    ] = False,
) -> None:
    """Create .vscode/sftp.json in an existing project.

    Edit the connection settings before syncing.
    """
    init_sftp_config_command(root_value=root, force=force, dry_run=dry_run)


@file_app.command(
    "pack", short_help="Create a portable offline sync ZIP from local files."
)
@command_errors
def pack_sync_bundle_cli(
    root: SyncBundlePackRootOption = None,
    profile: SFTPProfileOption = None,
    no_config: Annotated[
        bool,
        typer.Option(
            "--no-config",
            help="Ignore .vscode/sftp.json (mutually exclusive with --profile)",
        ),
    ] = False,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Output ZIP path (default: ./hgc-sync.zip)",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing output ZIP"),
    ] = False,
    git_changed: SyncBundleGitChangedOption = False,
    exclude: SyncBundleExcludeOption = None,
    dry_run: SyncBundleDryRunOption = False,
) -> None:
    """Pack a project snapshot into an offline ZIP. With --git-changed, the ZIP
    includes deletion markers for removed paths. An empty patch creates no ZIP.
    """
    pack_sync_bundle_command(
        root_value=root,
        profile=profile,
        no_config=no_config,
        output=output,
        force=force,
        git_changed=git_changed,
        exclude=exclude,
        dry_run=dry_run,
    )


@file_app.command("apply", short_help="Verify and apply a portable offline sync ZIP.")
@command_errors
def apply_sync_bundle_cli(
    bundle: Annotated[
        str,
        typer.Argument(help="Path to a sync ZIP created by hgc file pack"),
    ],
    root: SyncBundleApplyRootOption = None,
    delete: SyncBundleDeleteOption = False,
    skip_create: SyncBundleSkipCreateOption = False,
    ignore_existing: SyncBundleIgnoreExistingOption = False,
    update: SyncBundleUpdateOption = False,
    dry_run: SyncBundleDryRunOption = False,
) -> None:
    """Verify the entire offline bundle before writing to the destination.
    Bundle files win conflicts by default. With --delete, a full snapshot
    removes extra paths and a patch removes only paths marked for deletion.
    """
    apply_sync_bundle_command(
        bundle_value=bundle,
        root_value=root,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        dry_run=dry_run,
    )


CleanupRuleOption = Annotated[
    list[str] | None,
    typer.Option(
        "--rule",
        "-r",
        help="Rule ID (command prefix optional); repeat or add auto for automatic rules",
    ),
]


CleanupYesOption = Annotated[
    bool,
    typer.Option(
        "--yes",
        "-y",
        help="Select every eligible candidate and skip both deletion prompts",
    ),
]


@file_app.command("rule", short_help="List cleanup rules or inspect a full rule ID.")
@command_errors
def cleanup_rules_cli(
    rule_id: Annotated[
        str | None, typer.Argument(help="Full rule ID to inspect (no shorthand)")
    ] = None,
    command: Annotated[
        str | None, typer.Option("--command", help="Filter rules by clean or purge")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="Explicit cleanup TOML")
    ] = None,
) -> None:
    """List effective cleanup rules, including disabled rules.

    Supply a full rule ID to inspect its settings.
    """
    if command is not None and command not in CLEANUP_COMMANDS:
        raise typer.BadParameter("choose clean or purge", param_hint="--command")
    try:
        snapshot = load_rules(config)
    except CleanupConfigError as exc:
        die(str(exc))
    rules = tuple(
        rule for rule in snapshot.rules if command is None or rule.command == command
    )
    if rule_id is not None:
        rules = tuple(rule for rule in rules if rule.id == rule_id)
        if not rules:
            filter_hint = f" under --command {command}" if command is not None else ""
            raise typer.BadParameter(
                f"no matching rule with full ID {rule_id!r}{filter_hint}",
                param_hint="RULE_ID",
            )
    render_cleanup_rules(
        rules, detail=rule_id is not None, resolved=snapshot.expressions
    )


@file_app.command("purge", short_help="Find and remove rebuildable project artifacts.")
@command_errors
def space_purge_cli(
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            help="Optional directories to scan instead of configured roots",
            metavar="PATH...",
            autocompletion=complete_directory,
        ),
    ] = None,
    rule: CleanupRuleOption = None,
    yes: CleanupYesOption = False,
    scan_timeout: Annotated[
        int | None,
        typer.Option(
            "--scan-timeout",
            min=0,
            help="Shared generic scan budget in seconds; 0 or omitted means unlimited",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Preview selected removals without changing files"
        ),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive",
            "-R",
            help="Discover candidates without the default depth limit of 6",
        ),
    ] = False,
    include_unverified: Annotated[
        bool,
        typer.Option(
            "--include-unverified",
            help="Include rule-matched artifacts with unverified project context or purpose",
        ),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Explicit cleanup TOML; otherwise use Hagency repository rules",
        ),
    ] = None,
    edit_paths: Annotated[
        bool,
        typer.Option("--paths", help="Edit the configured purge scan directories"),
    ] = False,
) -> None:
    """Permanently delete rebuildable artifacts. Scan explicit paths, saved paths,
    or automatic project roots, in that order. Git-tracked data is protected.

    An interactive run asks for selection and confirmation. A non-interactive
    run previews unless --yes is supplied. --dry-run always previews.
    Use --paths on its own to edit saved scan roots.
    """
    selected_paths = list(paths or [])
    if any(not value.strip() for value in selected_paths):
        raise typer.BadParameter("scan paths must not be empty", param_hint="PATH")
    if edit_paths and (
        selected_paths
        or dry_run
        or recursive
        or include_unverified
        or config is not None
        or rule is not None
        or scan_timeout is not None
        or yes
    ):
        raise typer.BadParameter(
            "--paths cannot be combined with PATH, --dry-run, --recursive, --include-unverified, --config, --rule, --scan-timeout or --yes",
            param_hint="--paths",
        )

    try:
        if edit_paths:
            report = edit_purge_paths()
            render_paths_edit_report(report)
        else:
            from hagency_cli.commands.cleanup_ui import QuestionaryCleanupUI

            request = PurgeRequest(
                paths=tuple(
                    Path(os.path.abspath(expand_path(value, Path.cwd())))
                    for value in selected_paths
                ),
                dry_run=dry_run,
                recursive=recursive,
                include_unverified=include_unverified,
                config=config,
                rules=tuple(rule or ()),
                scan_timeout=scan_timeout or 0,
                yes=yes,
            )
            ui = QuestionaryCleanupUI()
            report = purge_space(request, ui=ui)
            render_cleanup_report(report, presented_plan=ui.presented_plan)
    except CleanupSelectionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except OSError as exc:
        die(f"could not complete file purge: {exc}")

    if report.exit_code:
        raise typer.Exit(report.exit_code)


@file_app.command(
    "clean", short_help="Remove obsolete Agent versions and configured cache files."
)
@command_errors
def clean_files_cli(
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            help="Directories limiting rule entries, or supplying entries for rootless rules",
            metavar="PATH...",
            autocompletion=complete_directory,
        ),
    ] = None,
    rule: CleanupRuleOption = None,
    yes: CleanupYesOption = False,
    scan_timeout: Annotated[
        int,
        typer.Option(
            "--scan-timeout",
            min=0,
            help="Shared paths scan budget in seconds; 0 means unlimited (Agent sources keep 60 seconds)",
        ),
    ] = 0,
    agent: Annotated[
        list[str] | None,
        typer.Option(
            "--agent",
            help="Agent to inspect: codex, claude or copilot; repeat to select several",
        ),
    ] = None,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive",
            "-R",
            help="Remove the depth limit for referenced purge rules",
        ),
    ] = False,
    include_unverified: Annotated[
        bool,
        typer.Option(
            "--include-unverified",
            help="Include referenced purge artifacts with unverified project context or purpose",
        ),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Explicit cleanup TOML; otherwise use Hagency repository rules",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Preview selected removals without changing files"
        ),
    ] = False,
) -> None:
    """Permanently remove files selected by cleanup rules. An interactive run
    asks for selection and confirmation. A non-interactive run previews unless
    --yes is supplied. --dry-run always previews.

    Agent cleanup requires Linux/WSL. On Windows, select project or path
    rules explicitly with --rule. Choose either --agent or --rule.

    \b
      hgc file clean -r empty_directories ./ --dry-run
    """
    if agent is not None and rule is not None:
        raise typer.BadParameter("--agent and --rule are mutually exclusive")
    if any(not value.strip() for value in paths or ()):
        raise typer.BadParameter("scan paths must not be empty", param_hint="PATH")
    selected = tuple(dict.fromkeys(agent or []))
    if any(value not in AGENTS for value in selected):
        raise typer.BadParameter(
            f"choose an Agent from {', '.join(AGENTS)}", param_hint="--agent"
        )
    try:
        from hagency_cli.commands.cleanup_ui import QuestionaryCleanupUI

        ui = QuestionaryCleanupUI()
        report = clean_files(
            CleanRequest(
                recursive=recursive,
                include_unverified=include_unverified,
                agents=selected,
                scan_timeout=scan_timeout,
                yes=yes,
                dry_run=dry_run,
                config=config,
                rules=tuple(rule or ()),
                paths=tuple(
                    Path(os.path.abspath(expand_path(value, Path.cwd())))
                    for value in paths or ()
                ),
            ),
            ui=ui,
        )
        render_cleanup_report(report, presented_plan=ui.presented_plan)
    except CleanupSelectionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except OSError as exc:
        die(f"could not complete file clean: {exc}")
    if report.exit_code:
        raise typer.Exit(report.exit_code)
