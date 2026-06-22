from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .common import die, expand_path, read_toml, render_toml, write_toml
from .profiles import (
    build_profile_config,
    discover_skill_dirs,
    discover_skill_links,
    init_profile,
    list_profile_configs,
    profile_config_path,
    profile_dir_path,
    profile_skill_names,
    profile_source_names,
    read_profile_config,
    remove_profile_directory,
    resolve_selector,
    resolve_profile_skill_reference,
    skill_source,
    source_relative_selector,
    update_profile_config,
    validate_profile_skill_selectors,
    validate_profile_name,
    workspace_source,
    write_profile_config,
)
from .sources import (
    add_source_entry,
    build_source_entry,
    find_profile_source_references,
    infer_owner_source_name_from_url,
    is_git_url,
    raw_source_by_name,
    remove_source_entry,
    require_source_path,
    resolve_sources,
    resolve_source_add_args,
    select_sources,
    sync_source,
)
from .workspace import init_workspace, resolve_workspace_root, workspace_config_path


def add_root_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", "-r", help="Hagency workspace root")


def add_source_resolution_options(parser: argparse.ArgumentParser) -> None:
    add_root_option(parser)
    parser.add_argument("--checkout-dir", help="Override defaults.checkout_dir")


def add_dry_run_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without changing files")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_source_slice(value: str, total: int) -> list[int]:
    def parse_index(raw: str, label: str) -> int:
        try:
            parsed = int(raw)
        except ValueError:
            die(f"invalid source slice {value!r}: {label} must be a positive integer")
        if parsed <= 0:
            die(f"invalid source slice {value!r}: {label} must be a positive integer")
        return parsed

    indexes: set[int] = set()
    for term in value.split(","):
        if not term:
            die(f"invalid source slice: {value}")
        if ":" in term:
            parts = term.split(":")
            if len(parts) != 2 or (not parts[0] and not parts[1]):
                die(f"invalid source slice: {value}")
            start = 1 if not parts[0] else parse_index(parts[0], "start")
            end = total if not parts[1] else parse_index(parts[1], "end")
        else:
            start = parse_index(term, "index")
            end = start

        if start > end:
            die(f"invalid source slice {value!r}: start must be <= end")
        if start > total or end > total:
            die(f"invalid source slice {value!r}: selected source count is {total}")
        indexes.update(range(start, end + 1))
    return sorted(indexes)


def source_slice_entries(selected: list, value: str | None) -> list[tuple[int, object]]:
    total = len(selected)
    if value is None:
        indexes = set(range(1, total + 1))
    else:
        indexes = set(parse_source_slice(value, total))
    return [(index, source) for index, source in enumerate(selected, start=1) if index in indexes]


def format_called_process_error(error: subprocess.CalledProcessError) -> str:
    cmd = error.cmd
    if isinstance(cmd, list | tuple):
        rendered_cmd = " ".join(str(part) for part in cmd)
    else:
        rendered_cmd = str(cmd)
    details = (error.stderr or error.output or "").strip()
    if details:
        return f"command failed with exit {error.returncode}: {rendered_cmd}: {details}"
    return f"command failed with exit {error.returncode}: {rendered_cmd}"


def flatten_option_values(values: list[list[str]] | None) -> list[str] | None:
    if not values:
        return None
    return [item for group in values for item in group]


def add_profile_skill_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include", "-i", nargs="+", action="append", help="Skill selectors to include")
    parser.add_argument("--exclude", "-e", nargs="+", action="append", help="Skill selectors to exclude")


def dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def workspace_root_arg(value: str | None) -> Path:
    return resolve_workspace_root(value, Path.cwd())


def load_registry(args: argparse.Namespace, root: Path) -> dict:
    return read_toml(workspace_config_path(root))


def load_sources(args: argparse.Namespace, root: Path) -> dict:
    registry = load_registry(args, root)
    return resolve_sources(registry, repo_root=root, checkout_override=getattr(args, "checkout_dir", None))


def default_sync_depth(registry: dict) -> int | None:
    depth = registry.get("defaults", {}).get("depth")
    if depth is None:
        return None
    if isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
        die("defaults.depth must be a positive integer")
    return depth


def init_workspace_command(args: argparse.Namespace) -> None:
    init_workspace(args.root, Path.cwd(), force=args.force, dry_run=args.dry_run)


def sync_sources_with_progress(entries: list[tuple[int, object]], *, total: int, dry_run: bool, depth: int | None) -> None:
    failures: list[str] = []
    for index, source in entries:
        print(f"sync source [{index}/{total}] {source.name}")
        try:
            sync_source(source, dry_run=dry_run, depth=depth)
        except subprocess.CalledProcessError as exc:
            failures.append(source.name)
            print(f"Error: source {source.name} failed: {format_called_process_error(exc)}", file=sys.stderr)

    if failures:
        die(f"source sync failed for: {', '.join(failures)}")


def sync_selected_sources(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    registry = load_registry(args, root)
    sources = resolve_sources(registry, repo_root=root, checkout_override=getattr(args, "checkout_dir", None))
    depth = args.depth if args.depth is not None else default_sync_depth(registry)

    selected_names = list(args.names)
    if args.profile:
        profile = read_profile_config(root, args.profile)
        selected_names.extend(profile_source_names(profile))

    selected = select_sources(sources, selected_names) if selected_names else list(sources.values())
    total = len(selected)
    sync_sources_with_progress(source_slice_entries(selected, args.slice), total=total, dry_run=args.dry_run, depth=depth)


def profile_init_link_mode(args: argparse.Namespace) -> str:
    if args.copy and args.link_mode == "symlink":
        die("-cp cannot be combined with --link-mode symlink")
    if args.copy:
        return "copy"
    return args.link_mode or "symlink"


def init_profile_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    sources = load_sources(args, root)
    profile = read_profile_config(root, args.name)
    target = expand_path(args.path, Path.cwd())
    init_profile(profile, sources, root, target, link_mode=profile_init_link_mode(args), dry_run=args.dry_run)


def skill_skip_roots(source_name: str, sources: dict) -> set[Path] | None:
    if source_name == "workspace":
        return {source.path for source in sources.values()}
    return None


def format_skill_row(source_name: str, source, name: str, target: Path) -> str:
    selector = source_relative_selector(source, target)
    return "\t".join([source_name, name, selector, str(target.resolve())])


def list_all_skill_rows(root: Path, sources: dict) -> list[str]:
    rows = []
    candidates = [("workspace", workspace_source(root)), *sources.items()]
    for source_name, source in candidates:
        if not source.path.exists():
            print(f"Warning: skipping missing source {source_name}: {source.path}", file=sys.stderr)
            continue
        if not source.path.is_dir():
            print(f"Warning: skipping non-directory source {source_name}: {source.path}", file=sys.stderr)
            continue
        skip_roots = skill_skip_roots(source_name, sources)
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            rows.append(format_skill_row(source_name, source, target.name, target))
    return rows


def validate_skill_source_filters(source_filters: list[str], sources: dict, root: Path) -> list[str]:
    selected = dedupe_preserve_order(source_filters)
    available = {"workspace": workspace_source(root), **sources}
    for source_name in selected:
        source = available.get(source_name)
        if source is None:
            die(f"unknown source: {source_name}")
        require_source_path(source)
    return selected


def list_filtered_skill_rows(root: Path, sources: dict, source_filters: list[str]) -> list[str]:
    rows = []
    available = {"workspace": workspace_source(root), **sources}
    for source_name in validate_skill_source_filters(source_filters, sources, root):
        source = available[source_name]
        skip_roots = skill_skip_roots(source_name, sources)
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            rows.append(format_skill_row(source_name, source, target.name, target))
    return rows


def list_selector_links(source, selector: str, *, skip_roots: set[Path] | None = None) -> list[tuple[str, Path]]:
    if selector == "*":
        return discover_skill_links(source, skip_roots=skip_roots)
    return resolve_selector(source, selector, skip_roots=skip_roots)


def list_profile_selected_links(config: dict, source, *, skip_roots: set[Path] | None = None) -> list[tuple[str, Path]]:
    includes = config.get("include") or ["*"]
    excludes = config.get("exclude") or []

    links = []
    for item in includes:
        links.extend(list_selector_links(source, item, skip_roots=skip_roots))

    excluded_paths = set()
    for item in excludes:
        for _name, target in list_selector_links(source, item, skip_roots=skip_roots):
            excluded_paths.add(target.resolve())

    return [(name, target) for name, target in links if target.resolve() not in excluded_paths]


def list_profile_skill_rows(root: Path, sources: dict, profile: dict, source_filters: list[str] | None) -> list[str]:
    selected_sources = set(validate_skill_source_filters(source_filters, sources, root)) if source_filters else None
    rows = []
    workspace = workspace_source(root)
    for source_name, config in profile.get("skill", {}).items():
        if selected_sources is not None and source_name not in selected_sources:
            continue
        source = skill_source(source_name, sources, workspace)
        require_source_path(source)
        skip_roots = skill_skip_roots(source_name, sources)
        for name, target in list_profile_selected_links(config or {}, source, skip_roots=skip_roots):
            rows.append(format_skill_row(source_name, source, name, target))
    return rows


def skill_list_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    sources = load_sources(args, root)
    source_filters = args.sources or []

    if args.profile:
        profile = read_profile_config(root, args.profile)
        rows = list_profile_skill_rows(root, sources, profile, source_filters)
    elif source_filters:
        rows = list_filtered_skill_rows(root, sources, source_filters)
    else:
        rows = list_all_skill_rows(root, sources)

    print("source\tname\tselector\tpath")
    for row in rows:
        print(row)


def profile_list_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    print("name\tdescription\tskills")
    for name, profile in list_profile_configs(root):
        description = profile.get("description") or "-"
        skills = ",".join(profile_skill_names(profile)) or "-"
        print(f"{name}\t{description}\t{skills}")


def profile_show_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    profile = read_profile_config(root, args.name)
    print(render_toml(profile).rstrip())


def validate_profile_skill_args(args: argparse.Namespace) -> tuple[list[str] | None, list[str] | None]:
    include = flatten_option_values(args.include)
    exclude = flatten_option_values(args.exclude)
    if (include or exclude) and not getattr(args, "add_skill", None):
        die("--include and --exclude require --add-skill")
    return include, exclude


def with_inferred_include(include: list[str] | None, selector: str | None) -> list[str] | None:
    if selector is None:
        return include
    values = [selector]
    for item in include or []:
        if item not in values:
            values.append(item)
    return values


def profile_add_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    validate_profile_name(args.name)
    profile_dir = profile_dir_path(root, args.name)
    if profile_dir.exists():
        die(f"profile already exists: {args.name}")
    include, exclude = validate_profile_skill_args(args)
    sources = load_sources(args, root) if args.add_skill else {}
    add_skill = args.add_skill
    if add_skill:
        add_skill, inferred_include = resolve_profile_skill_reference(
            add_skill,
            sources,
            root,
            command_prefix=f"hagency profile add {args.name}",
            option="-AS",
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(add_skill, sources, root, include=include, exclude=exclude)
    profile = build_profile_config(
        args.name,
        description=args.description,
        add_skill=add_skill,
        include=include,
        exclude=exclude,
        sources=sources,
    )

    if args.dry_run:
        print(f"Would create profile: {profile_config_path(root, args.name)}")
        print(render_toml(profile).rstrip())
        return

    write_profile_config(root, args.name, profile)
    print(f"added profile: {args.name}")


def profile_update_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    include, exclude = validate_profile_skill_args(args)
    if args.replace and not args.add_skill:
        die("--replace requires --add-skill")
    profile = read_profile_config(root, args.name)
    sources = load_sources(args, root) if args.add_skill or args.remove_skill else {}
    add_skill = args.add_skill
    if add_skill:
        add_skill, inferred_include = resolve_profile_skill_reference(
            add_skill,
            sources,
            root,
            command_prefix=f"hagency profile update {args.name}",
            option="-AS",
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(add_skill, sources, root, include=include, exclude=exclude)
    remove_skill = args.remove_skill
    remove_skill_selector = None
    if remove_skill:
        remove_skill, inferred_remove = resolve_profile_skill_reference(
            remove_skill,
            sources,
            root,
            command_prefix=f"hagency profile update {args.name}",
            option="-RS",
        )
        if inferred_remove is not None:
            validate_profile_skill_selectors(remove_skill, sources, root, include=[inferred_remove], exclude=None)
            remove_skill_selector = (remove_skill, inferred_remove)
            remove_skill = None
    updated = update_profile_config(
        profile,
        description=args.description,
        add_skill=add_skill,
        remove_skill=remove_skill,
        remove_skill_selector=remove_skill_selector,
        include=include,
        exclude=exclude,
        replace=args.replace,
        sources=sources,
    )

    if args.dry_run:
        print(f"Would update profile: {profile_config_path(root, args.name)}")
        print(render_toml(updated).rstrip())
        return

    write_profile_config(root, args.name, updated)
    print(f"updated profile: {args.name}")


def profile_remove_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    profile_dir = profile_dir_path(root, args.name)
    if not profile_dir.exists():
        die(f"unknown profile: {args.name}")

    if args.dry_run:
        print(f"Would remove profile directory: {profile_dir}")
        return

    remove_profile_directory(root, args.name)
    print(f"removed profile: {args.name}")


def source_kind(source) -> str:
    return "remote" if source.remote else "local"


def source_list_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    sources = load_sources(args, root)
    print("name\ttype\tpath\turl\tref")
    for name, source in sources.items():
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


def source_show_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    registry = load_registry(args, root)
    sources = resolve_sources(registry, repo_root=root, checkout_override=args.checkout_dir)
    source = sources.get(args.name)
    if source is None:
        die(f"unknown source: {args.name}")
    raw_source = raw_source_by_name(registry, args.name) or {}
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


def source_add_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    config_path = workspace_config_path(root)
    registry = load_registry(args, root)
    resolve_sources(registry, repo_root=root, checkout_override=None)
    name, url = resolve_source_add_args(args.source, name=args.name, url=args.url)
    if (
        is_git_url(args.source)
        and args.name is None
        and args.url is None
        and raw_source_by_name(registry, name) is not None
    ):
        owner_name = infer_owner_source_name_from_url(args.source)
        if not owner_name or owner_name == name:
            die(
                f"source already exists: {name}; could not infer owner/repo name from URL; "
                "pass --name <custom-name>"
            )
        if raw_source_by_name(registry, owner_name) is not None:
            die(
                f"source already exists: {name}; owner-prefixed source also exists: {owner_name}; "
                "pass --name <custom-name>"
            )
        name = owner_name
    source_name, entry = build_source_entry(
        name=name,
        url=url,
        path=args.path,
        ref=args.ref,
        remote_name=args.remote_name,
    )
    add_source_entry(registry, source_name, entry)
    sync_source_entry = None
    sync_depth = None
    if args.sync:
        sync_depth = default_sync_depth(registry)
        sync_source_entry = select_sources(
            resolve_sources(registry, repo_root=root, checkout_override=None),
            [source_name],
        )[0]

    if args.dry_run:
        print("Would add source:")
        print(render_toml({"source": {source_name: entry}}).rstrip())
        if sync_source_entry is not None:
            sync_sources_with_progress([(1, sync_source_entry)], total=1, dry_run=True, depth=sync_depth)
        return

    write_toml(config_path, registry)
    print(f"added source: {source_name}")
    if sync_source_entry is not None:
        sync_sources_with_progress([(1, sync_source_entry)], total=1, dry_run=False, depth=sync_depth)


def source_remove_command(args: argparse.Namespace) -> None:
    root = workspace_root_arg(args.root)
    config_path = workspace_config_path(root)
    registry = load_registry(args, root)
    resolve_sources(registry, repo_root=root, checkout_override=None)

    references = find_profile_source_references(root, args.name)
    if references and not args.force:
        refs = ", ".join(str(path.relative_to(root)) for path in references)
        die(f"source {args.name} is referenced by {refs}; pass --force to remove only the config entry")

    remove_source_entry(registry, args.name)
    if args.dry_run:
        print(f"Would remove source: {args.name}")
        return

    write_toml(config_path, registry)
    print(f"removed source: {args.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hagency", description="Manage Hagency workspaces, profiles, and sources.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="Initialize a Hagency workspace.")
    add_root_option(init_parser)
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing workspace config")
    add_dry_run_option(init_parser)
    init_parser.set_defaults(func=init_workspace_command)

    source_parser = subcommands.add_parser("source", aliases=["s"], help="Manage workspace sources.")
    source_subcommands = source_parser.add_subparsers(dest="source_command", required=True)

    list_parser = source_subcommands.add_parser("list", aliases=["ls"], help="List configured sources.")
    add_source_resolution_options(list_parser)
    list_parser.set_defaults(func=source_list_command)

    show_parser = source_subcommands.add_parser("show", help="Show one configured source.")
    show_parser.add_argument("name", help="Source name")
    add_source_resolution_options(show_parser)
    show_parser.set_defaults(func=source_show_command)

    add_parser = source_subcommands.add_parser(
        "add",
        help="Add a source to the workspace config. Pass a Git URL directly to infer the source name.",
    )
    add_parser.add_argument("source", help="Source name, or Git URL to infer the name from")
    add_parser.add_argument("--name", help="Override inferred source name when source is a Git URL")
    add_parser.add_argument("--url", help="Git remote URL")
    add_parser.add_argument("--path", help="Explicit local or checkout path")
    add_parser.add_argument("--ref", help="Git branch, tag, or ref")
    add_parser.add_argument("--remote-name", help="Git remote name")
    add_parser.add_argument("--sync", action="store_true", help="Sync the added source after writing the config")
    add_root_option(add_parser)
    add_dry_run_option(add_parser)
    add_parser.set_defaults(func=source_add_command)

    remove_parser = source_subcommands.add_parser("remove", aliases=["rm"], help="Remove a source from the workspace config.")
    remove_parser.add_argument("name", help="Source name")
    remove_parser.add_argument("--force", action="store_true", help="Remove even if profiles reference the source")
    add_root_option(remove_parser)
    add_dry_run_option(remove_parser)
    remove_parser.set_defaults(func=source_remove_command)

    sync_parser = source_subcommands.add_parser("sync", help="Sync external sources.")
    sync_parser.add_argument("names", nargs="*", help="Optional source names to sync")
    sync_parser.add_argument("--profile", help="Sync only sources referenced by profiles/<name>/config.toml")
    sync_parser.add_argument("--depth", type=positive_int, help="Create or update shallow checkouts with this depth")
    sync_parser.add_argument("--slice", "-s", help="1-based source indexes or slices to sync, such as 4:, 2:4, :3, 4, or 1,3:")
    add_source_resolution_options(sync_parser)
    add_dry_run_option(sync_parser)
    sync_parser.set_defaults(func=sync_selected_sources)

    skill_parser = subcommands.add_parser("skill", help="List workspace and source skills.")
    skill_subcommands = skill_parser.add_subparsers(dest="skill_command", required=True)

    skill_list_parser = skill_subcommands.add_parser("list", aliases=["ls"], help="List discovered skills.")
    skill_list_parser.add_argument("--source", "-s", dest="sources", action="append", help="Limit to a source name or workspace")
    skill_list_parser.add_argument("--profile", "-p", help="Limit to skills selected by a profile")
    add_source_resolution_options(skill_list_parser)
    skill_list_parser.set_defaults(func=skill_list_command)

    profile_parser = subcommands.add_parser("profile", aliases=["p"], help="Manage profiles.")
    profile_subcommands = profile_parser.add_subparsers(dest="profile_command", required=True)

    profile_list_parser = profile_subcommands.add_parser("list", aliases=["ls"], help="List profiles.")
    add_root_option(profile_list_parser)
    profile_list_parser.set_defaults(func=profile_list_command)

    profile_add_parser = profile_subcommands.add_parser("add", help="Add a profile.")
    profile_add_parser.add_argument("name", help="Profile name under profiles/")
    profile_add_parser.add_argument("--description", help="Profile description")
    profile_add_parser.add_argument(
        "-AS",
        "--add-skill",
        help="Source, skill name, or SOURCE:selector to add to this profile",
    )
    add_profile_skill_options(profile_add_parser)
    add_root_option(profile_add_parser)
    add_dry_run_option(profile_add_parser)
    profile_add_parser.set_defaults(func=profile_add_command)

    profile_update_parser = profile_subcommands.add_parser("update", aliases=["u"], help="Update a profile.")
    profile_update_parser.add_argument("name", help="Profile name under profiles/")
    profile_update_parser.add_argument("--description", help="Profile description")
    skill_update_group = profile_update_parser.add_mutually_exclusive_group()
    skill_update_group.add_argument(
        "-AS",
        "--add-skill",
        help="Source, skill name, or SOURCE:selector to add or merge",
    )
    skill_update_group.add_argument(
        "-RS",
        "--remove-skill",
        help="Source, skill name, or SOURCE:selector to remove",
    )
    add_profile_skill_options(profile_update_parser)
    profile_update_parser.add_argument("--replace", action="store_true", help="Replace one profile skill entry")
    add_root_option(profile_update_parser)
    add_dry_run_option(profile_update_parser)
    profile_update_parser.set_defaults(func=profile_update_command)

    profile_remove_parser = profile_subcommands.add_parser("remove", aliases=["rm"], help="Remove a profile.")
    profile_remove_parser.add_argument("name", help="Profile name under profiles/")
    add_root_option(profile_remove_parser)
    add_dry_run_option(profile_remove_parser)
    profile_remove_parser.set_defaults(func=profile_remove_command)

    profile_show_parser = profile_subcommands.add_parser("show", help="Show one profile config.")
    profile_show_parser.add_argument("name", help="Profile name under profiles/")
    add_root_option(profile_show_parser)
    profile_show_parser.set_defaults(func=profile_show_command)

    profile_init_parser = profile_subcommands.add_parser("init", help="Initialize a profile into a target directory.")
    profile_init_parser.add_argument("--path", "-p", required=True, help="Target workspace directory")
    profile_init_parser.add_argument("name", help="Profile name under profiles/")
    profile_init_parser.add_argument("-cp", dest="copy", action="store_true", help="Copy skill directories instead of symlinking")
    profile_init_parser.add_argument("--link-mode", choices=["symlink", "copy"], help="How to materialize profile skills")
    add_source_resolution_options(profile_init_parser)
    add_dry_run_option(profile_init_parser)
    profile_init_parser.set_defaults(func=init_profile_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
