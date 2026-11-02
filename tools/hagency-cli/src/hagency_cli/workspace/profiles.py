from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import WorkspaceError, fail
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.skills import (
    SkillConflictUI,
    SkillLinkCandidate,
    SkillScan,
    install_skill,
    resolve_link_name_conflicts,
    resolve_selector,
    skill_skip_roots,
    skill_source,
    validate_skill_selector,
    validate_skill_source,
    validate_skills_dir,
    workspace_skill_scans,
    workspace_source,
)
from hagency_cli.workspace.sources import Source, source_path_warning


def find_profile_source_references(repo_root: Path, source_name: str) -> list[Path]:
    profiles_root = repo_root / "profiles"
    if not profiles_root.exists():
        return []

    references: list[Path] = []
    for profile_config in sorted(profiles_root.glob("*/config.toml")):
        profile = read_toml(profile_config)
        if "skills" in profile:
            fail(
                "legacy [[skills]] profile config is no longer supported; use [skill.<source>]"
            )
        if source_name in profile.get("skill", {}):
            references.append(profile_config)
    return references


def validate_profile_name(profile_name: str) -> None:
    if (
        not profile_name
        or profile_name in {".", ".."}
        or "/" in profile_name
        or "\\" in profile_name
    ):
        fail(f"unsafe profile name: {profile_name}")


def profiles_root_path(repo_root: Path) -> Path:
    return repo_root / "profiles"


def profile_dir_path(repo_root: Path, profile_name: str) -> Path:
    validate_profile_name(profile_name)
    return profiles_root_path(repo_root) / profile_name


def profile_config_path(repo_root: Path, profile_name: str) -> Path:
    return profile_dir_path(repo_root, profile_name) / "config.toml"


def require_profile_schema(profile: dict) -> None:
    if "skills" in profile:
        fail(
            "legacy [[skills]] profile config is no longer supported; use [skill.<source>]"
        )
    if not isinstance(profile.get("skill", {}), dict):
        fail("profile skill must be a table")


def read_profile_config(repo_root: Path, profile_name: str) -> dict:
    profile = read_toml(profile_config_path(repo_root, profile_name))
    require_profile_schema(profile)
    return profile


def write_profile_config(repo_root: Path, profile_name: str, profile: dict) -> None:
    path = profile_config_path(repo_root, profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_toml(path, profile)


def list_profile_configs(
    repo_root: Path, *, progress: Progress | None = None
) -> list[tuple[str, dict]]:
    profiles_root = profiles_root_path(repo_root)
    if not profiles_root.exists():
        return []

    profiles = []
    for config_path in sorted(
        profiles_root.glob("*/config.toml"), key=lambda path: path.parent.name
    ):
        try:
            profile = read_profile_config(repo_root, config_path.parent.name)
        except WorkspaceError as exc:
            if progress is None:
                raise
            emit_event(
                progress,
                f"Warning: profile {config_path.parent.name}: {exc}",
                severity="warning",
            )
            continue
        profiles.append((config_path.parent.name, profile))
    return profiles


def profile_source_names(profile: dict) -> list[str]:
    names = []
    require_profile_schema(profile)
    for source_name in profile.get("skill", {}):
        if source_name != "workspace":
            names.append(source_name)
    return names


def profile_skill_names(profile: dict) -> list[str]:
    require_profile_schema(profile)
    return list(profile.get("skill", {}))


def dedupe_append(existing: list[str], additions: list[str]) -> list[str]:
    values = list(existing)
    seen = set(values)
    for item in additions:
        if item not in seen:
            values.append(item)
            seen.add(item)
    return values


def require_string_list(value: object, *, field: str, source_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail(f"profile skill {source_name} field {field} must be a string list")
    return value


def set_profile_skill(
    profile: dict,
    source_name: str,
    *,
    include: list[str] | None,
    exclude: list[str] | None,
    replace: bool,
) -> None:
    require_profile_schema(profile)
    skills = profile.setdefault("skill", {})
    existing = skills.get(source_name)

    if replace or existing is None:
        config: dict[str, list[str]] = {}
        if include:
            config["include"] = dedupe_append([], include)
        if exclude:
            config["exclude"] = dedupe_append([], exclude)
        skills[source_name] = config
        return

    if not isinstance(existing, dict):
        fail(f"profile skill {source_name} must be a table")
    config = dict(existing)
    if include:
        current_include = config.get("include")
        if current_include is not None:
            include_values = require_string_list(
                current_include, field="include", source_name=source_name
            )
            if "*" not in include_values:
                config["include"] = dedupe_append(include_values, include)
    if exclude:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(
                current_exclude, field="exclude", source_name=source_name
            )
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, exclude)
    skills[source_name] = config


def remove_profile_skill(profile: dict, source_name: str) -> None:
    require_profile_schema(profile)
    skills = profile.get("skill", {})
    if source_name not in skills:
        fail(f"profile does not reference skill source: {source_name}")
    del skills[source_name]
    if not skills:
        profile.pop("skill", None)


def remove_profile_skill_selector(
    profile: dict, source_name: str, selector: str, *, wildcard_exclusion: bool = True
) -> None:
    require_profile_schema(profile)
    skills = profile.get("skill", {})
    if source_name not in skills:
        fail(f"profile does not reference skill source: {source_name}")

    if not isinstance(skills[source_name], dict):
        fail(f"profile skill {source_name} must be a table")
    config = dict(skills[source_name])
    current_include = config.get("include")
    include_values = (
        require_string_list(current_include, field="include", source_name=source_name)
        if current_include is not None
        else []
    )
    if current_include is None or ("*" in include_values and wildcard_exclusion):
        if selector != "*" and selector in include_values:
            config["include"] = [item for item in include_values if item != selector]
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(
                current_exclude, field="exclude", source_name=source_name
            )
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, [selector])
        skills[source_name] = config
        return

    remaining = [item for item in include_values if item != selector]
    if len(remaining) == len(include_values):
        fail(f"profile skill source {source_name} does not include skill: {selector}")
    if remaining:
        config["include"] = remaining
        skills[source_name] = config
        return

    del skills[source_name]
    if not skills:
        profile.pop("skill", None)


def build_profile_config(
    profile_name: str,
    *,
    description: str | None,
    add_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    sources: dict[str, Source],
) -> dict:
    profile = {"name": profile_name}
    if description is not None:
        profile["description"] = description
    if add_skill:
        validate_skill_source(add_skill, sources)
        set_profile_skill(
            profile, add_skill, include=include, exclude=exclude, replace=True
        )
    return profile


def update_profile_config(
    profile: dict,
    *,
    description: str | None,
    add_skill: str | None,
    remove_skill: str | None,
    remove_skill_selector: tuple[str, str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
    replace: bool,
    sources: dict[str, Source],
    wildcard_exclusion: bool = True,
) -> dict:
    require_profile_schema(profile)
    updated = dict(profile)
    if "skill" in profile:
        updated["skill"] = dict(profile["skill"])

    changed = False
    if description is not None:
        updated["description"] = description
        changed = True
    if add_skill:
        validate_skill_source(add_skill, sources)
        set_profile_skill(
            updated, add_skill, include=include, exclude=exclude, replace=replace
        )
        changed = True
    if remove_skill:
        remove_profile_skill(updated, remove_skill)
        changed = True
    if remove_skill_selector:
        remove_profile_skill_selector(
            updated, *remove_skill_selector, wildcard_exclusion=wildcard_exclusion
        )
        changed = True
    if not changed:
        fail("profile update requires at least one change")
    return updated


def remove_profile_directory(repo_root: Path, profile_name: str) -> Path:
    profile_dir = profile_dir_path(repo_root, profile_name)
    if not profile_dir.exists():
        fail(f"unknown profile: {profile_name}")
    shutil.rmtree(profile_dir)
    return profile_dir


def validate_profile_skill_selectors(
    source_name: str,
    sources: dict[str, Source],
    workspace_root: Path,
    *,
    include: list[str] | None,
    exclude: list[str] | None,
    progress: Progress | None = None,
    scan: SkillScan | None = None,
) -> None:
    selectors = [*(include or []), *(exclude or [])]
    source = skill_source(source_name, sources, workspace_source(workspace_root))
    for selector in selectors:
        validate_skill_selector(source, selector)
    if unavailable := source_path_warning(source):
        emit_event(
            progress,
            f"Warning: {unavailable}; skill selection is unverified",
            severity="warning",
        )
        return
    scan = scan or SkillScan(source, skill_skip_roots(source_name, sources))
    for selector in selectors:
        resolve_selector(source, selector, scan=scan, progress=progress)


@dataclass
class ProfileSelection:
    links: list[SkillLinkCandidate] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def inspect_profile(
    profile_name: str,
    profile: dict,
    scans: dict[str, SkillScan],
    *,
    invalid_sources: dict[str, str] | None = None,
    progress: Progress | None = None,
    allow_name_conflicts: bool = True,
) -> ProfileSelection:
    require_profile_schema(profile)
    selection = ProfileSelection()
    for source_name, config in profile.get("skill", {}).items():
        context = f"profile {profile_name}, source {source_name}"
        if not isinstance(config, dict):
            selection.problems.append(f"{context}: skill entry must be a table")
            continue
        scan = scans.get(source_name)
        unavailable = source_path_warning(scan.source) if scan is not None else None

        includes = []
        excluded = set()
        for key in ("include", "exclude"):
            try:
                values = require_string_list(
                    config.get(key, []), field=key, source_name=source_name
                )
            except WorkspaceError as exc:
                selection.problems.append(f"{context}: {exc}")
                continue
            for selector in values or (["*"] if key == "include" else []):
                label = f"{context}, {key} {selector!r}"
                if scan is None:
                    reason = (invalid_sources or {}).get(
                        source_name, f"unknown source: {source_name}"
                    )
                    selection.problems.append(f"{label}: {reason}")
                    continue
                try:
                    if unavailable:
                        validate_skill_selector(scan.source, selector)
                        selection.problems.append(f"{label}: {unavailable}")
                        continue
                    links = resolve_selector(
                        scan.source,
                        selector,
                        scan=scan,
                        progress=progress,
                        warning_context=f"{label}: ",
                        allow_name_conflicts=allow_name_conflicts or selector == "*",
                    )
                except WorkspaceError as exc:
                    selection.problems.append(f"{label}: {exc}")
                    continue
                for name, target in links:
                    if key == "include":
                        includes.append(SkillLinkCandidate(name, source_name, target))
                    else:
                        excluded.add(target.resolve())
        selection.links.extend(
            link for link in includes if link.target.resolve() not in excluded
        )
    return selection


def apply_profile(
    profile: dict,
    sources: dict[str, Source],
    workspace_root: Path,
    skills_dir: Path,
    *,
    link_mode: str,
    dry_run: bool,
    profile_name: str | None = None,
    invalid_sources: dict[str, str] | None = None,
    conflict_ui: SkillConflictUI | None = None,
    progress: Progress | None = None,
) -> tuple[SkillLinkCandidate, ...]:
    validate_skills_dir(skills_dir)
    selection = inspect_profile(
        profile_name or profile.get("name", "<unnamed>"),
        profile,
        workspace_skill_scans(workspace_root, sources),
        invalid_sources=invalid_sources,
        progress=progress,
    )
    if selection.problems:
        fail("\n".join(selection.problems))
    selected = resolve_link_name_conflicts(
        selection.links, conflict_ui, preview_conflicts=dry_run, progress=progress
    )
    for link in selected:
        install_skill(
            skills_dir,
            link.name,
            link.target,
            link_mode=link_mode,
            dry_run=dry_run,
            progress=progress,
        )
    return tuple(selected)
