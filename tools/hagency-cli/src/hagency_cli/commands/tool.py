from __future__ import annotations

from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_profile,
    complete_tool,
)
from hagency_cli.commands.options import WorkspaceRootOption
from hagency_cli.commands.shared import (
    aliased_command,
    command_errors,
    make_app,
    render_event,
    require_at_most_one,
)
from hagency_cli.workspace.context import workspace_root_arg
from hagency_cli.workspace.operations.tools import forget_installation, manage_tool
from hagency_cli.workspace.tool_catalog import catalog, edit_tool, probe, select_tools

tool_app = make_app(
    help_text="Manage workspace and profile tools.",
    add_completion=False,
)

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


@aliased_command(
    tool_app, "tool", "list", short_help="List tools and their status on PATH."
)
@command_errors
def tool_list(
    name: Annotated[str | None, typer.Argument(autocompletion=complete_tool)] = None,
    profile: Profile = None,
    root: WorkspaceRootOption = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", help="Exit 1 if any tool is unavailable or fails its version requirement"
        ),
    ] = False,
    no_probe: Annotated[
        bool,
        typer.Option("--no-probe", help="Show configuration only (incompatible with --strict)"),
    ] = False,
) -> None:
    """List tools and run each selected tool's version command on PATH (up to 60 seconds each)."""
    require_at_most_one({"--strict": strict, "--no-probe": no_probe})
    selected = select_tools(catalog(workspace_root_arg(root), profile), name)
    failed = False
    for index, tool in enumerate(selected):
        heading = tool.name
        required = tool.version if tool.version != "*" else ""
        path = ""
        if not no_probe:
            status = probe(tool)
            detected = status.status in {"available", "version-mismatch", "version-unknown"}
            label = "Detected" if detected else "Undetected"
            version = f" v{status.version}" if status.version is not None else ""
            heading = f"{label}: {tool.name}{version}"
            if status.path is not None:
                path = str(status.path)
            if status.status == "available":
                required = ""
            else:
                failed = True
        if index:
            print()
        print(heading)
        for key, value in (
            ("command", tool.command),
            ("configured_version", required),
            ("path", path),
            ("install", ", ".join(tool.methods)),
            ("package", tool.package),
        ):
            if value and value != tool.name:
                print(f"    {key}: {value}")
    if strict and failed:
        raise typer.Exit(1)


@aliased_command(tool_app, "tool", "add", short_help="Register or edit a tool.")
@command_errors
def tool_add(
    name: str,
    profile: Profile = None,
    root: WorkspaceRootOption = None,
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
    """Register a tool or edit its supplied fields. Profiles inherit other fields
    from the workspace declaration.

    \b
      hgc tool add playwright -i pnpm -i npm
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
    name: Name, profile: Profile = None, root: WorkspaceRootOption = None, dry_run: DryRun = False
) -> None:
    """Remove a tool declaration. Removing an inherited tool from a profile disables it there."""
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
    root: WorkspaceRootOption = None,
    install_dir: Directory = None,
    dry_run: DryRun = False,
    extra: Extra = None,
) -> None:
    """Reuse a compatible installation or install the newest compatible release.
    Pass extra installer arguments after --.

    \b
      hgc tool install markitdown --install-dir ./tools -- --offline
    """
    _change("install", name, profile, root, install_dir, dry_run, extra)


@aliased_command(tool_app, "tool", "upgrade", short_help="Upgrade an installed tool.")
@command_errors
def tool_upgrade(
    name: Name,
    profile: Profile = None,
    root: WorkspaceRootOption = None,
    install_dir: Directory = None,
    dry_run: DryRun = False,
    extra: Extra = None,
) -> None:
    """Upgrade a recorded or package-manager-owned tool within its configured range."""
    _change("upgrade", name, profile, root, install_dir, dry_run, extra)


@tool_app.command(
    "uninstall", short_help="Uninstall a tool and retain its declaration."
)
@command_errors
def tool_uninstall(
    name: Name,
    profile: Profile = None,
    root: WorkspaceRootOption = None,
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
    """Uninstall a verified package and keep its declaration.

    Use --install-dir to select among recorded copies.
    """
    if forget:
        if profile is not None:
            select_tools(catalog(workspace_root_arg(root), profile), name)
        elif root is not None:
            workspace_root_arg(root)
        forget_installation(name, install_dir, dry_run, render_event)
        return
    _change("uninstall", name, profile, root, install_dir, dry_run)
