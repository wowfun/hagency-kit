from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_install_selector,
    complete_profile,
    complete_skill_add,
    complete_source_or_workspace,
)
from hagency_cli.commands.options import WorkspaceRootOption
from hagency_cli.commands.shared import (
    aliased_command,
    command_errors,
    make_app,
    render_event,
    require_at_most_one,
)
from hagency_cli.workspace.catalog import discover_catalog
from hagency_cli.workspace.context import load_sources, workspace_root_arg
from hagency_cli.workspace.operations.skills import add_skills
from hagency_cli.workspace.profiles import read_profile_config

skill_app = make_app(
    help_text="Discover and install skills from local and Git sources.",
    add_completion=False,
)


def skill_list_command(
    *,
    source_filters: list[str],
    profile_name: str | None,
    root_value: str | None,
    checkout_dir: str | None,
) -> None:
    root = workspace_root_arg(root_value)
    sources = load_sources(root, checkout_dir)

    profile = read_profile_config(root, profile_name) if profile_name else None
    entries = discover_catalog(
        root,
        sources,
        profile=profile,
        source_filters=source_filters,
        progress=render_event,
    )
    for entry in entries:
        print(entry.reference)


@aliased_command(
    skill_app,
    "skill",
    "add",
    short_help="Install skills from a source or reference.",
)
@command_errors
def skill_add_cli(
    skill: Annotated[
        str,
        typer.Argument(
            help="Skill reference, source name, Git URL, owner/repo, or explicit local directory",
            autocompletion=complete_skill_add,
        ),
    ],
    selectors: Annotated[
        list[str] | None,
        typer.Option(
            "--skill",
            "-s",
            help="Source-relative selector (repeatable, mutually exclusive with --all)",
            autocompletion=complete_install_selector,
        ),
    ] = None,
    all_skills: Annotated[
        bool, typer.Option("--all", help="Select all discovered skills")
    ] = False,
    source_name: Annotated[
        str | None,
        typer.Option("--source-name", help="Name to register or reuse for a URL/path"),
    ] = None,
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref", help="Remote branch or tag; existing checkouts are never switched"
        ),
    ] = None,
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
    global_install: Annotated[
        bool,
        typer.Option("--global", help="Install under ~/.agents/skills"),
    ] = False,
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
    """Install selected skills into ./.agents/skills by default. Choose at most
    one of --path, --dir and --global. Existing links may be
    retargeted. Independent directories are preserved.

    For a source with multiple skills, select interactively or pass --skill
    or --all. URL inputs can register sources and fetch missing checkouts.
    A dry run previews without network access or writes.

    \b
      hgc skill add workspace:skills/analyze-diff
      hgc skill add owner/repo --skill nested/one -d ./project
    """
    require_at_most_one({"--skill": selectors, "--all": all_skills})
    require_at_most_one(
        {"--path": skills_path, "--dir": skills_root, "--global": global_install}
    )
    from hagency_cli.commands.skill_ui import QuestionarySkillConflictUI

    add_skills(
        cwd=Path.cwd(),
        ui=QuestionarySkillConflictUI(),
        selectors=tuple(selectors or ()),
        all_skills=all_skills,
        source_name=source_name,
        ref=ref,
        skill=skill,
        skills_path=skills_path,
        skills_root=skills_root,
        global_install=global_install,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
        progress=render_event,
    )


@aliased_command(skill_app, "skill", "list", short_help="List local skill references.")
@command_errors
def skill_list_cli(
    sources: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="Source name or workspace (repeatable)",
            autocompletion=complete_source_or_workspace,
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Limit to skills selected by a profile",
            autocompletion=complete_profile,
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
) -> None:
    """List locally discoverable skills, one SOURCE:selector per line. Pass a
    reference to hgc skill add. Source and profile filters can be combined.
    Use hgc source sync to obtain a missing checkout.
    """
    skill_list_command(
        source_filters=list(sources or []),
        profile_name=profile,
        root_value=root,
        checkout_dir=checkout_dir,
    )
