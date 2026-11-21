from __future__ import annotations

from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_profile,
    complete_tool,
)
from hagency_cli.commands.shared import (
    aliased_command,
    command_errors,
    make_app,
    render_event,
)
from hagency_cli.workspace.context import workspace_root_arg
from hagency_cli.workspace.operations.tools import forget_installation, manage_tool
from hagency_cli.workspace.tool_catalog import catalog, edit_tool, probe, select_tools

tool_app = make_app(
    help_text="""Manage workspace and profile tools.

    Register commands in TOML, check their availability, and manage global installations.

    \b
    Examples:
      hgc tool check --profile dev
      hgc tool install playwright
      hgc tool upgrade markitdown -- --offline
    """,
    add_completion=False,
)

Root = Annotated[
    str | None,
    typer.Option(
        "--root", "-r", autocompletion=complete_directory, help="Hagency workspace"
    ),
]
Profile = Annotated[
    str | None,
    typer.Option(
        "--profile",
        autocompletion=complete_profile,
        help="Use the effective profile catalog",
    ),
]
Name = Annotated[
    str, typer.Argument(autocompletion=complete_tool, help="Tool catalog name")
]
Directory = Annotated[
    str | None,
    typer.Option(
        "--install-dir",
        autocompletion=complete_directory,
        help="Exact installation root",
    ),
]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Preview without writing")]
Extra = Annotated[list[str] | None, typer.Argument(help="Installer arguments after --")]


@aliased_command(tool_app, "tool", "list", short_help="List configured tools.")
@command_errors
def tool_list(profile: Profile = None, root: Root = None) -> None:
    """List effective tool declarations and installer preference.

    \b
    Examples:
      hgc tool ls --profile dev
    """
    print("name\tcommand\tversion\tinstall\tpackage")
    for tool in catalog(workspace_root_arg(root), profile).values():
        print(
            f"{tool.name}\t{tool.command}\t{tool.version or '*'}\t{','.join(tool.methods)}\t{tool.package}"
        )


@tool_app.command("check", short_help="Check commands and versions on PATH.")
@command_errors
def tool_check(
    name: Annotated[str | None, typer.Argument(autocompletion=complete_tool)] = None,
    profile: Profile = None,
    root: Root = None,
) -> None:
    """Check one tool or every effective tool. Exit 1 if any are unavailable.

    \b
    Examples:
      hgc tool check
      hgc tool check ffmpeg --profile dev
    """
    failed = False
    print("name\tstatus\tversion\tpath")
    for tool in select_tools(catalog(workspace_root_arg(root), profile), name):
        status = probe(tool)
        print(status.display(tool.name))
        failed |= status.status != "available"
    if failed:
        raise typer.Exit(1)


@aliased_command(tool_app, "tool", "add", short_help="Register or edit a tool.")
@command_errors
def tool_add(
    name: str,
    profile: Profile = None,
    root: Root = None,
    dry_run: DryRun = False,
    version: Annotated[
        str | None, typer.Option(help="Exact version, comparison range or *")
    ] = None,
    install: Annotated[
        list[str] | None,
        typer.Option(
            "--install",
            "-i",
            "--method",
            help="Installer in preference order (repeatable)",
        ),
    ] = None,
    package: Annotated[
        str | None, typer.Option(help="Package name, optionally with uv extras")
    ] = None,
    command: Annotated[str | None, typer.Option(help="Executable name")] = None,
    version_arg: Annotated[
        list[str] | None, typer.Option(help="Version probe argument (repeatable)")
    ] = None,
    install_arg: Annotated[
        list[str] | None, typer.Option(help="Saved installer argument (repeatable)")
    ] = None,
) -> None:
    """Register or update fields in the workspace or selected profile.

    \b
    Examples:
      hgc tool add playwright --install pnpm --install npm
      hgc tool a ffmpeg --profile dev --version ">=7,<10"
    """
    print(
        edit_tool(
            workspace_root_arg(root),
            profile,
            name,
            {
                "version": version,
                "install": install,
                "package": package,
                "command": command,
                "version_args": version_arg,
                "install_args": install_arg,
            },
            dry_run,
        )
    )


@aliased_command(tool_app, "tool", "remove", short_help="Remove a tool declaration.")
@command_errors
def tool_remove(
    name: Name, profile: Profile = None, root: Root = None, dry_run: DryRun = False
) -> None:
    """Delete the declaration or disable an inherited entry in a profile.

    \b
    Examples:
      hgc tool rm ffmpeg --profile dev
    """
    print(edit_tool(workspace_root_arg(root), profile, name, None, dry_run))


def _change(action, name, profile, root, install_dir, dry_run, extra=()):
    (tool,) = select_tools(catalog(workspace_root_arg(root), profile), name)
    manage_tool(
        tool,
        action,
        install_dir=install_dir,
        extra=tuple(extra or ()),
        dry_run=dry_run,
        progress=render_event,
    )


@aliased_command(
    tool_app, "tool", "install", short_help="Ensure a compatible tool is installed."
)
@command_errors
def tool_install(
    name: Name,
    profile: Profile = None,
    root: Root = None,
    install_dir: Directory = None,
    dry_run: DryRun = False,
    extra: Extra = None,
) -> None:
    """Install the newest compatible release, or reuse a suitable existing tool.

    \b
    Examples:
      hgc tool i playwright
      hgc tool install markitdown --install-dir ./tools -- --offline
    """
    _change("install", name, profile, root, install_dir, dry_run, extra)


@aliased_command(tool_app, "tool", "upgrade", short_help="Upgrade an installed tool.")
@command_errors
def tool_upgrade(
    name: Name,
    profile: Profile = None,
    root: Root = None,
    install_dir: Directory = None,
    dry_run: DryRun = False,
    extra: Extra = None,
) -> None:
    """Upgrade a recorded or package-manager-owned installation within its version range.

    \b
    Examples:
      hgc tool update markitdown
      hgc tool u playwright --dry-run
    """
    _change("upgrade", name, profile, root, install_dir, dry_run, extra)


@tool_app.command(
    "uninstall", short_help="Uninstall a tool and retain its declaration."
)
@command_errors
def tool_uninstall(
    name: Name,
    profile: Profile = None,
    root: Root = None,
    install_dir: Directory = None,
    dry_run: DryRun = False,
    forget: Annotated[
        bool,
        typer.Option(
            "--forget",
            help="Discard the installation receipt and keep the files and PATH",
        ),
    ] = False,
) -> None:
    """Uninstall a tool after verifying the installation's package ownership.

    Use --forget to discard a receipt after a manual replacement or catalog removal.
    When several copies are recorded, select the receipt with --install-dir.

    \b
    Examples:
      hgc tool uninstall playwright --install-dir ./tools
    """
    if forget:
        if profile is not None:
            select_tools(catalog(workspace_root_arg(root), profile), name)
        elif root is not None:
            workspace_root_arg(root)
        forget_installation(name, install_dir, dry_run, render_event)
        return
    _change("uninstall", name, profile, root, install_dir, dry_run)
