from __future__ import annotations

from pathlib import Path

from hagency_cli.workspace.config import render_toml
from hagency_cli.workspace.context import load_sources, workspace_root_arg
from hagency_cli.workspace.errors import SkillReferenceError, WorkspaceError, fail
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.profiles import (
    apply_profile,
    build_profile_config,
    profile_config_path,
    profile_dir_path,
    read_profile_config,
    remove_profile_directory,
    update_profile_config,
    validate_profile_name,
    validate_profile_skill_selectors,
    write_profile_config,
)
from hagency_cli.workspace.skills import (
    LinkMode,
    SkillConflictUI,
    SkillLinkCandidate,
    resolve_link_mode,
    resolve_selector,
    resolve_skill_install_dir,
    resolve_skill_reference,
    workspace_skill_scans,
)


def apply_profile_to_directory(
    *,
    name: str,
    skills_path: str | None,
    skills_root: str | None,
    copy: bool,
    link_mode: LinkMode | None,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    conflict_ui: SkillConflictUI | None = None,
    progress: Progress | None = None,
) -> tuple[SkillLinkCandidate, ...]:

    invocation_cwd = Path.cwd()
    root = workspace_root_arg(root_value)
    invalid: dict[str, str] = {}
    sources = load_sources(root, checkout_dir, invalid=invalid)
    profile = read_profile_config(root, name)
    skills_dir = resolve_skill_install_dir(
        skills_path,
        skills_root,
        False,
        invocation_cwd,
        default_root=None,
    )
    return apply_profile(
        profile,
        sources,
        root,
        skills_dir,
        link_mode=resolve_link_mode(copy, link_mode),
        profile_name=name,
        invalid_sources=invalid,
        dry_run=dry_run,
        conflict_ui=conflict_ui,
        progress=progress,
    )


def validate_profile_skill_args(
    include: list[str] | None,
    exclude: list[str] | None,
    add_skill: str | None,
) -> tuple[list[str] | None, list[str] | None]:
    if (include or exclude) and not add_skill:
        fail("--include and --exclude require --add-skill")
    return include, exclude


def with_inferred_include(
    include: list[str] | None, selector: str | None
) -> list[str] | None:
    if selector is None:
        return include
    values = [selector]
    for item in include or []:
        if item not in values:
            values.append(item)
    return values


def add_profile(
    *,
    name: str,
    description: str | None,
    add_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> dict:
    root = workspace_root_arg(root_value)
    validate_profile_name(name)
    profile_dir = profile_dir_path(root, name)
    if profile_dir.exists():
        fail(f"profile already exists: {name}")
    include, exclude = validate_profile_skill_args(include, exclude, add_skill)
    sources = load_sources(root, checkout_dir) if add_skill else {}
    if add_skill:
        scans = workspace_skill_scans(root, sources)
        add_skill, inferred_include = resolve_skill_reference(
            add_skill,
            sources,
            root,
            scans=scans,
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(
            add_skill,
            sources,
            root,
            include=include,
            exclude=exclude,
            progress=progress,
            scan=scans[add_skill],
        )
    profile = build_profile_config(
        name,
        description=description,
        add_skill=add_skill,
        include=include,
        exclude=exclude,
        sources=sources,
    )

    if dry_run:
        emit_event(progress, f"Would create profile: {profile_config_path(root, name)}")
        emit_event(progress, render_toml(profile).rstrip())
        return profile

    write_profile_config(root, name, profile)
    emit_event(progress, f"added profile: {name}")
    return profile


def _resolve_removal(
    profile: dict,
    value: str,
    root: Path,
    checkout_dir: str | None,
) -> tuple[str, str | None]:
    saved = profile.get("skill", {})
    if value in saved:
        return value, None
    name, separator, selector = value.partition(":")
    matches = []
    for source_name, config in saved.items():
        if separator and name != source_name or not isinstance(config, dict):
            continue
        includes = config.get("include")
        if (
            isinstance(includes, list)
            and (selector if separator else value) in includes
        ):
            matches.append(source_name)
    if len(matches) == 1:
        return matches[0], selector if separator else value
    if matches:
        raise SkillReferenceError(value, tuple(f"{name}:{value}" for name in matches))

    sources = load_sources(root, checkout_dir)
    scans = workspace_skill_scans(root, sources)
    name, selector = resolve_skill_reference(value, sources, root, scans=scans)
    if selector is not None:
        validate_profile_skill_selectors(
            name, sources, root, include=[selector], exclude=None, scan=scans[name]
        )
    return name, selector


def _wildcard_exclusion_needed(
    profile: dict, source_name: str, selector: str, root: Path, checkout_dir: str | None
) -> bool:
    entry = profile.get("skill", {}).get(source_name, {})
    includes = entry.get("include", []) if isinstance(entry, dict) else []
    if (
        not isinstance(includes, list)
        or selector == "*"
        or selector not in includes
        or "*" not in includes
    ):
        return True
    # A literal include alongside "*" needs an exclude only if the wildcard
    # would still install it. A stale exclude would itself block application.
    sources = load_sources(root, checkout_dir, invalid={})
    scan = workspace_skill_scans(root, sources).get(source_name)
    if scan is None or not scan.source.path.is_dir():
        return False
    try:
        matches = resolve_selector(
            scan.source, selector, scan=scan, allow_name_conflicts=True
        )
    except WorkspaceError:
        return False
    active = {path.resolve() for path in scan.active}
    return any(path.resolve() in active for _, path in matches)


def update_profile(
    *,
    name: str,
    description: str | None,
    add_skill: str | None,
    remove_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    replace: bool,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> dict:
    root = workspace_root_arg(root_value)
    include, exclude = validate_profile_skill_args(include, exclude, add_skill)
    if replace and not add_skill:
        fail("--replace requires --add-skill")
    profile = read_profile_config(root, name)
    sources = load_sources(root, checkout_dir) if add_skill else {}
    if add_skill:
        scans = workspace_skill_scans(root, sources)
        add_skill, inferred_include = resolve_skill_reference(
            add_skill, sources, root, scans=scans
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(
            add_skill,
            sources,
            root,
            include=include,
            exclude=exclude,
            progress=progress,
            scan=scans[add_skill],
        )
    remove_skill_selector = None
    wildcard_exclusion = True
    if remove_skill:
        source_name, selector = _resolve_removal(
            profile, remove_skill, root, checkout_dir
        )
        if selector is None:
            remove_skill = source_name
        else:
            wildcard_exclusion = _wildcard_exclusion_needed(
                profile, source_name, selector, root, checkout_dir
            )
            remove_skill_selector = (source_name, selector)
            remove_skill = None
    updated = update_profile_config(
        profile,
        description=description,
        add_skill=add_skill,
        remove_skill=remove_skill,
        remove_skill_selector=remove_skill_selector,
        wildcard_exclusion=wildcard_exclusion,
        include=include,
        exclude=exclude,
        replace=replace,
        sources=sources,
    )

    if dry_run:
        emit_event(progress, f"Would update profile: {profile_config_path(root, name)}")
        emit_event(progress, render_toml(updated).rstrip())
        return updated

    write_profile_config(root, name, updated)
    emit_event(progress, f"updated profile: {name}")
    return updated


def remove_profile(
    *,
    name: str,
    root_value: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> Path:
    root = workspace_root_arg(root_value)
    profile_dir = profile_dir_path(root, name)
    if not profile_dir.exists():
        fail(f"unknown profile: {name}")

    if dry_run:
        emit_event(progress, f"Would remove profile directory: {profile_dir}")
        return profile_dir

    remove_profile_directory(root, name)
    emit_event(progress, f"removed profile: {name}")
    return profile_dir
