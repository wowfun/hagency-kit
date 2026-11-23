from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_profile,
    complete_profile_remove_reference,
    complete_selector,
    complete_skill_reference,
)
from hagency_cli.commands.options import WorkspaceRootOption
from hagency_cli.commands.shared import (
    aliased_command,
    command_errors,
    diagnostic,
    make_app,
    render_event,
    require_at_most_one,
)
from hagency_cli.workspace.config import read_toml_document
from hagency_cli.workspace.context import load_sources, workspace_root_arg
from hagency_cli.workspace.operations.profiles import (
    add_profile,
    apply_profile_to_directory,
    remove_profile,
    update_profile,
)
from hagency_cli.workspace.profiles import (
    inspect_profile,
    list_profile_configs,
    profile_config_path,
    profile_skill_names,
    require_profile_schema,
)
from hagency_cli.workspace.skills import LinkMode, SkillScan, workspace_skill_scans

profile_app = make_app(
    help_text="Manage and apply workspace profiles.",
    add_completion=False,
)


def _report_profile(
    name: str, profile: dict, scans: dict[str, SkillScan], invalid: dict[str, str]
) -> None:
    result = inspect_profile(
        name, profile, scans, invalid_sources=invalid, progress=render_event
    )
    for problem in result.problems:
        diagnostic(f"Warning: {problem}", "warning")


def profile_list_command(*, root_value: str | None) -> None:
    root = workspace_root_arg(root_value)
    invalid: dict[str, str] = {}
    scans = workspace_skill_scans(root, load_sources(root, None, invalid=invalid))
    print("name\tdescription\tskills")
    for name, profile in list_profile_configs(root, progress=render_event):
        _report_profile(name, profile, scans, invalid)
        description = profile.get("description") or "-"
        skills = ",".join(profile_skill_names(profile)) or "-"
        print(f"{name}\t{description}\t{skills}")


def profile_show_command(*, name: str, root_value: str | None) -> None:
    root = workspace_root_arg(root_value)
    text, profile = read_toml_document(profile_config_path(root, name))
    require_profile_schema(profile)
    invalid: dict[str, str] = {}
    scans = workspace_skill_scans(root, load_sources(root, None, invalid=invalid))
    _report_profile(name, profile, scans, invalid)
    print(text.rstrip())


@aliased_command(profile_app, "profile", "list", short_help="List profiles and their sources.")
@command_errors
def profile_list_cli(
    root: WorkspaceRootOption = None,
) -> None:
    """List profiles, their descriptions and selected sources."""
    profile_list_command(root_value=root)


@aliased_command(profile_app, "profile", "add", short_help="Add a profile.")
@command_errors
def profile_add_cli(
    name: Annotated[str, typer.Argument(help="Profile name under profiles/")],
    description: Annotated[
        str | None, typer.Option("--description", help="Profile description")
    ] = None,
    add_skill: Annotated[
        str | None,
        typer.Option(
            "-AS",
            "--add-skill",
            help="Source, skill name, or SOURCE:selector to add to this profile",
            autocompletion=complete_skill_reference,
        ),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            "-i",
            help="Skill selectors to include",
            autocompletion=complete_selector,
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            "-e",
            help="Skill selectors to exclude",
            autocompletion=complete_selector,
        ),
    ] = None,
    root: WorkspaceRootOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing"),
    ] = False,
) -> None:
    """Create a profile with an optional skill selection. An exact skill reference
    selects that skill. A source selects all its skills unless filtered.
    --include and --exclude require --add-skill.

    \b
      hgc profile add review -AS workspace:skills/analyze-diff
    """
    add_profile(
        name=name,
        description=description,
        add_skill=add_skill,
        include=include,
        exclude=exclude,
        root_value=root,
        checkout_dir=None,
        dry_run=dry_run,
        progress=render_event,
    )


@aliased_command(profile_app, "profile", "update", short_help="Edit a description or skill selection.")
@command_errors
def profile_update_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    description: Annotated[
        str | None, typer.Option("--description", help="Profile description")
    ] = None,
    add_skill: Annotated[
        str | None,
        typer.Option(
            "-AS",
            "--add-skill",
            help="Source, skill name, or SOURCE:selector to add or merge",
            autocompletion=complete_skill_reference,
        ),
    ] = None,
    remove_skill: Annotated[
        str | None,
        typer.Option(
            "-RS",
            "--remove-skill",
            help="Source, skill name, or SOURCE:selector to remove",
            autocompletion=complete_profile_remove_reference,
        ),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            "-i",
            help="Skill selectors to include",
            autocompletion=complete_selector,
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            "-e",
            help="Skill selectors to exclude",
            autocompletion=complete_selector,
        ),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace one profile skill entry")
    ] = False,
    root: WorkspaceRootOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing"),
    ] = False,
) -> None:
    """Edit a saved profile. Add and remove are mutually exclusive. Includes,
    excludes and --replace require --add-skill. Apply the profile separately
    to install its selection.

    \b
      hgc profile update review -AS workspace -i skills/log-analyzer --replace
    """
    require_at_most_one({"--add-skill": add_skill, "--remove-skill": remove_skill})
    update_profile(
        name=name,
        description=description,
        add_skill=add_skill,
        remove_skill=remove_skill,
        include=include,
        exclude=exclude,
        replace=replace,
        root_value=root,
        checkout_dir=None,
        dry_run=dry_run,
        progress=render_event,
    )


@aliased_command(profile_app, "profile", "remove", short_help="Remove a profile.")
@command_errors
def profile_remove_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    root: WorkspaceRootOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing"),
    ] = False,
) -> None:
    """Delete the entire profile directory. Installed skills and source checkouts are retained."""
    remove_profile(name=name, root_value=root, dry_run=dry_run, progress=render_event)


@profile_app.command("show", short_help="Print the saved profile TOML.")
@command_errors
def profile_show_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    root: WorkspaceRootOption = None,
) -> None:
    """Print a profile's saved TOML. Local reference warnings go to stderr."""
    profile_show_command(name=name, root_value=root)


@aliased_command(
    profile_app,
    "profile",
    "apply",
    short_help="Install profile skills and optional tools.",
)
@command_errors
def profile_apply_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    skills_path: Annotated[
        str | None,
        typer.Option(
            "--path",
            "-p",
            metavar="PATH",
            help="Exact skills directory",
            autocompletion=complete_directory,
        ),
    ] = None,
    with_tools: Annotated[
        bool, typer.Option("--with-tools", "-T", help="Also install effective profile tools")
    ] = False,
    install_dir: Annotated[
        str | None,
        typer.Option("--install-dir", help="Installation root for tools", autocompletion=complete_directory),
    ] = None,
    skills_root: Annotated[
        str | None,
        typer.Option(
            "--dir",
            "-d",
            metavar="DIR",
            help="Install under DIR/.agents/skills",
            autocompletion=complete_directory,
        ),
    ] = None,
    copy: Annotated[
        bool, typer.Option("-cp", help="Copy skill directories instead of linking")
    ] = False,
    link_mode: Annotated[
        LinkMode | None,
        typer.Option(
            "--link-mode",
            help="Install method (default: junction on Windows, symlink elsewhere)",
        ),
    ] = None,
    root: WorkspaceRootOption = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Checkout base for sources without an explicit path",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing"),
    ] = False,
) -> None:
    """Install the profile's skills into ./.agents/skills by default. Choose
    either --path or --dir. Existing links may be retargeted. Independent
    directories and previously installed, unselected skills are kept.

    Source checkouts must already exist. Resolve duplicate-name conflicts
    interactively or narrow the profile selection. With --with-tools, tool
    installation follows skill installation and reports failures together.
    """
    require_at_most_one({"--path": skills_path, "--dir": skills_root})
    if install_dir and not with_tools:
        raise typer.BadParameter("--install-dir requires --with-tools/-T")
    from hagency_cli.commands.skill_ui import QuestionarySkillConflictUI

    apply_profile_to_directory(
        cwd=Path.cwd(),
        with_tools=with_tools,
        install_dir=install_dir,
        conflict_ui=QuestionarySkillConflictUI(),
        name=name,
        skills_path=skills_path,
        skills_root=skills_root,
        copy=copy,
        link_mode=link_mode,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
        progress=render_event,
    )
