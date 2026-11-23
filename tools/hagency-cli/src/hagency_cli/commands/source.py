from __future__ import annotations

from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_profile,
    complete_source,
)
from hagency_cli.commands.options import WorkspaceRootOption
from hagency_cli.commands.shared import (
    aliased_command,
    command_errors,
    diagnostic,
    die,
    make_app,
    render_event,
)
from hagency_cli.workspace.catalog import source_health_warning
from hagency_cli.workspace.context import (
    load_registry,
    load_sources,
    workspace_root_arg,
)
from hagency_cli.workspace.operations.sources import (
    add_source,
    remove_source,
    sync_selected_sources,
)
from hagency_cli.workspace.sources import raw_source_by_name, resolve_sources

source_app = make_app(
    help_text="Manage registered skill sources and their checkouts.",
    add_completion=False,
)


def source_kind(source) -> str:
    return "remote" if source.remote else "local"


def source_list_command(*, root_value: str | None, checkout_dir: str | None) -> None:
    root = workspace_root_arg(root_value)
    invalid: dict[str, str] = {}
    sources = load_sources(root, checkout_dir, invalid=invalid)
    for name, issue in invalid.items():
        diagnostic(f"Warning: invalid source {name}: {issue}", "warning")
    print("name\ttype\tpath\turl\tref")
    for name, source in sources.items():
        if issue := source_health_warning(source):
            diagnostic(f"Warning: {issue}", "warning")
        remote = source.remote
        print(
            "\t".join(
                [
                    name,
                    source_kind(source),
                    str(source.path),
                    remote.url if remote else "-",
                    remote.ref if remote else "-",
                ]
            )
        )


def source_show_command(
    *, name: str, root_value: str | None, checkout_dir: str | None
) -> None:
    root = workspace_root_arg(root_value)
    registry = load_registry(root)
    sources = resolve_sources(
        registry, repo_root=root, checkout_override=checkout_dir, only=name
    )
    source = sources.get(name)
    if source is None:
        die(f"unknown source: {name}")
    if issue := source_health_warning(source):
        diagnostic(f"Warning: {issue}", "warning")
    raw_source = raw_source_by_name(registry, name) or {}
    remote = source.remote
    raw_remote = raw_source.get("remote") or {}

    print(f"name: {source.name}")
    print(f"type: {source_kind(source)}")
    print(f"resolved_path: {source.path}")
    if "path" in raw_source:
        print(f"path: {raw_source['path']}")
    if remote:
        print(f"remote.url: {raw_remote.get('url', remote.url)}")
        print(f"remote.name: {raw_remote.get('name', remote.name)}")
        print(f"remote.ref: {raw_remote.get('ref', remote.ref)}")


@aliased_command(source_app, "source", "list", short_help="List configured sources.")
@command_errors
def source_list_cli(
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
    """List registered sources and their resolved local paths, including missing checkouts."""
    source_list_command(root_value=root, checkout_dir=checkout_dir)


@source_app.command("show", short_help="Show one configured source.")
@command_errors
def source_show_cli(
    name: Annotated[
        str, typer.Argument(help="Source name", autocompletion=complete_source)
    ],
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
    """Show the selected source's settings, resolved path and local availability."""
    source_show_command(name=name, root_value=root, checkout_dir=checkout_dir)


@aliased_command(
    source_app, "source",
    "add",
    short_help="Register a local or remote source.",
)
@command_errors
def source_add_cli(
    source: Annotated[
        str, typer.Argument(help="Source name, or Git URL to infer the name from")
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--name", help="Override inferred source name when source is a Git URL"
        ),
    ] = None,
    url: Annotated[str | None, typer.Option("--url", help="Git remote URL")] = None,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            help="Explicit local or checkout path",
            autocompletion=complete_directory,
        ),
    ] = None,
    ref: Annotated[
        str | None, typer.Option("--ref", help="Git branch, tag, or ref")
    ] = None,
    remote_name: Annotated[
        str | None, typer.Option("--remote-name", help="Git remote name")
    ] = None,
    sync: Annotated[
        bool,
        typer.Option("--sync", help="Sync the added source after writing the config"),
    ] = False,
    root: WorkspaceRootOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing"),
    ] = False,
) -> None:
    """Register a local directory or Git repository. Relative source paths resolve
    from the workspace root. With --sync, a failed checkout leaves the source
    registered. A dry run previews registration and sync without fetching.

    \b
      hgc source add my-skills --path ./shared/skills
      hgc source add https://github.com/owner/repo.git --sync
    """
    add_source(
        source_value=source,
        name_value=name,
        url_value=url,
        path_value=path,
        ref_value=ref,
        remote_name=remote_name,
        sync=sync,
        root_value=root,
        dry_run=dry_run,
        progress=render_event,
    )


@aliased_command(source_app, "source", "remove", short_help="Remove a source from the workspace config.")
@command_errors
def source_remove_cli(
    name: Annotated[
        str, typer.Argument(help="Source name", autocompletion=complete_source)
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Remove even if profiles reference the source"),
    ] = False,
    root: WorkspaceRootOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing"),
    ] = False,
) -> None:
    """Unregister a source and keep its checkout and installed skills. With
    --force, any profiles using the source retain their references.
    """
    remove_source(
        name=name, force=force, root_value=root, dry_run=dry_run, progress=render_event
    )


@source_app.command("sync", short_help="Update persistent Git source checkouts.")
@command_errors
def source_sync_cli(
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help="Source names to sync", autocompletion=complete_source
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Include sources selected by this profile",
            autocompletion=complete_profile,
        ),
    ] = None,
    depth: Annotated[
        int | None,
        typer.Option(
            "--depth", min=1, help="Create or update shallow checkouts with this depth"
        ),
    ] = None,
    source_slice: Annotated[
        str | None,
        typer.Option(
            "--slice",
            "-s",
            help="1-based indexes or inclusive ranges, e.g. 1,3:5",
        ),
    ] = None,
    reanchor: Annotated[
        bool,
        typer.Option(
            "--reanchor",
            help="Reset clean diverged branches to upstream, discarding local-only commits",
        ),
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
        typer.Option("--dry-run", help="Preview Git operations without fetching or writing"),
    ] = False,
) -> None:
    """Sync all registered sources or the union of the given names and profile
    sources. Local sources are checked in place. Git checkouts fast-forward by
    default. Sync continues after a source fails and returns an error if any failed.
    """
    sync_selected_sources(
        names=list(names or []),
        profile_name=profile,
        depth=depth,
        source_slice=source_slice,
        reanchor=reanchor,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
        progress=render_event,
    )
