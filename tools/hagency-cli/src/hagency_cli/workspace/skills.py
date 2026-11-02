from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol

from hagency_cli.paths import expand_path
from hagency_cli.workspace.errors import (
    SkillNameConflictError,
    SkillReferenceError,
    SkillSymlinkError,
    SourceNotReadyError,
    fail,
)
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.sources import Source

ARCHIVE_DIRS = {"archived", "archive", "deprecated"}

SKIP_DISCOVERY_DIRS = {
    ".agents",
    ".codex",
    ".git",
    ".hg",
    ".local",
    ".references",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
}


@dataclass(frozen=True)
class SkillLinkCandidate:
    name: str
    source_name: str
    target: Path


class SkillConflictUI(Protocol):
    def is_interactive(self) -> bool: ...

    def select(
        self, name: str, candidates: tuple[SkillLinkCandidate, ...]
    ) -> Path | None: ...


def validate_skill_source(source_name: str, sources: dict[str, Source]) -> None:
    if source_name == "workspace" or source_name in sources:
        return
    fail(f"unknown source: {source_name}")


def iter_skill_name_matches(
    skill_name: str,
    sources: dict[str, Source],
    workspace_root: Path,
    scans: dict[str, SkillScan] | None = None,
) -> list[tuple[str, Path]]:
    scans = (
        scans if scans is not None else workspace_skill_scans(workspace_root, sources)
    )
    matches: list[tuple[str, Path]] = []
    for source_name, scan in scans.items():
        if not scan.source.path.is_dir():
            continue
        for target in scan.active:
            if target.name == skill_name:
                matches.append((source_name, target))
    return matches


def split_source_selector_reference(
    value: str, sources: dict[str, Source]
) -> tuple[str, str] | None:
    source_name, separator, selector = value.partition(":")
    if not separator or (source_name != "workspace" and source_name not in sources):
        return None
    if not selector:
        fail(f"skill reference {value!r} is missing selector after ':'")
    return (source_name, selector)


def source_for_reference(
    source_name: str, sources: dict[str, Source], workspace_root: Path
) -> Source:
    if source_name == "workspace":
        return workspace_source(workspace_root)
    return sources[source_name]


def source_relative_selector(source: Source, target: Path) -> str:
    try:
        return target.resolve().relative_to(source.path.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def resolve_skill_reference(
    value: str,
    sources: dict[str, Source],
    workspace_root: Path,
    *,
    scans: dict[str, SkillScan] | None = None,
) -> tuple[str, str | None]:
    source_selector = split_source_selector_reference(value, sources)
    if source_selector is not None:
        return source_selector

    if value == "workspace" or value in sources:
        return (value, None)

    scans = (
        scans if scans is not None else workspace_skill_scans(workspace_root, sources)
    )
    matches = iter_skill_name_matches(value, sources, workspace_root, scans)
    if not matches:
        archived = [
            f"{scan.source.name}:{source_relative_selector(scan.source, path)}"
            for scan in scans.values()
            for path in scan.archived
            if path.name == value
        ]
        if archived:
            fail(
                f"unknown source or skill: {value}; deprecated skills require an exact path: {', '.join(archived)}"
            )
        unsynced = sorted(
            source.name
            for source in sources.values()
            if source.remote is not None and not source.path.exists()
        )
        if unsynced:
            raise SourceNotReadyError(
                f"unknown source or skill: {value}; unsynced sources may contain it",
                tuple(unsynced),
            )
        fail(f"unknown source or skill: {value}")
    if len(matches) > 1:
        references = tuple(
            f"{name}:{source_relative_selector(source_for_reference(name, sources, workspace_root), path)}"
            for name, path in matches
        )
        raise SkillReferenceError(value, references)
    source_name, _path = matches[0]
    return (source_name, value)


def is_archived_path(path: Path) -> bool:
    return any(
        part.casefold() in ARCHIVE_DIRS for part in Path(os.path.normpath(path)).parts
    )


def is_archived_location(path: Path) -> bool:
    return is_archived_path(path) or is_archived_path(path.resolve())


def _linked_directory(path: Path, canonical: Path) -> bool:
    if os.name != "nt":
        return path.is_symlink()
    try:
        info = path.lstat()
    except OSError:
        return True
    if not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    # Cloud files may be reparse points without redirecting the directory.
    return (
        info.st_reparse_tag
        in {stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT}
        or path.resolve() != canonical
    )


def _iter_skill_dirs(
    root: Path, skip_roots: set[Path], *, pruned: list[Path] | None
) -> Iterator[Path]:
    resolved_root = root.resolve()
    if any(resolved_root.is_relative_to(skip) for skip in skip_roots):
        return
    if pruned is not None and (
        is_archived_path(root) or is_archived_path(resolved_root)
    ):
        pruned.append(root)
        return

    for current, dirnames, filenames in os.walk(root):
        parent = Path(current)
        canonical_parent = resolved_root / parent.relative_to(root)
        children = []
        for name in dirnames:
            child = parent / name
            canonical = canonical_parent / name
            if name in SKIP_DISCOVERY_DIRS or _linked_directory(child, canonical):
                continue
            # With directory links pruned, descendants share the canonical root.
            if canonical in skip_roots:
                continue
            if pruned is not None and name.casefold() in ARCHIVE_DIRS:
                pruned.append(child)
            else:
                children.append(name)
        dirnames[:] = children
        marker = parent / "SKILL.md"
        if "SKILL.md" in filenames and marker.is_file():
            location = marker.resolve()
            if location.is_relative_to(resolved_root) and not any(
                location.is_relative_to(skip) for skip in skip_roots
            ):
                yield parent


def _walk_skills(
    root: Path, skip_roots: set[Path], *, pruned: list[Path] | None
) -> list[Path]:
    return sorted(_iter_skill_dirs(root, skip_roots, pruned=pruned), key=str)


def source_skill_content(source: Source) -> Literal["active", "archived", "empty"]:
    """Probe a registered source, stopping at the first skill in either category."""
    archives: list[Path] = []
    if next(_iter_skill_dirs(source.path, set(), pruned=archives), None) is not None:
        return "active"
    for root in archives:
        if next(_iter_skill_dirs(root, set(), pruned=None), None) is not None:
            return "archived"
    return "empty"


class SkillScan:
    """A command-local inventory. Scan archived subtrees only for diagnostics."""

    def __init__(self, source: Source, skip_roots: set[Path] | None = None):
        self.source = source
        self.skip_roots = {path.resolve() for path in (skip_roots or set())}
        self._archive_roots: list[Path] = []
        self._explicit_subtrees: dict[Path, list[Path]] = {}

    @cached_property
    def active(self) -> list[Path]:
        return _walk_skills(
            self.source.path, self.skip_roots, pruned=self._archive_roots
        )

    @cached_property
    def archived(self) -> list[Path]:
        # Populate the pruned roots even when diagnostics are requested first.
        _ = self.active
        return [
            path
            for root in self._archive_roots
            for path in _walk_skills(root, self.skip_roots, pruned=None)
        ]

    def paths_under(self, path: Path) -> list[Path]:
        root = path.resolve()
        if not self.contains(path) or is_archived_location(path):
            return []
        if root == self.source.path.resolve():
            return self.active
        # An explicit subtree can be inside a directory normally skipped at the
        # source root. Preserve scoped discovery there without broadening queries.
        relative = root.relative_to(self.source.path.resolve())
        if SKIP_DISCOVERY_DIRS.intersection(relative.parts):
            if root not in self._explicit_subtrees:
                self._explicit_subtrees[root] = _walk_skills(
                    root, self.skip_roots, pruned=[]
                )
            return self._explicit_subtrees[root]
        return [
            target for target in self.active if target.resolve().is_relative_to(root)
        ]

    def contains(self, path: Path) -> bool:
        target = path.resolve()
        return target.is_relative_to(self.source.path.resolve()) and not any(
            target.is_relative_to(skip) for skip in self.skip_roots
        )


def discover_skill_dirs(
    root: Path, *, skip_roots: set[Path] | None = None
) -> list[Path]:
    return _walk_skills(root, {path.resolve() for path in skip_roots or ()}, pruned=[])


def deprecated_skill_message(source: Source, path: Path) -> str:
    return f"deprecated skill {source.name}:{source_relative_selector(source, path)} ({path})"


def validate_skill_selector(source: Source, selector: str) -> Path:
    """Keep selectors relative to the source, including through symlink parents."""
    if (
        not isinstance(selector, str)
        or not selector
        or Path(selector).is_absolute()
        or PureWindowsPath(selector).drive
        or selector.startswith("\\")
    ):
        fail(f"skill selector {selector!r} must be relative to source {source.name}")
    target = source.path / selector
    try:
        root = source.path.resolve()
        # Also reject Windows traversal in portable profile configurations when
        # validating on POSIX, where backslashes would otherwise be literal.
        portable_target = source.path / selector.replace("\\", "/")
        contained = target.resolve().is_relative_to(
            root
        ) and portable_target.resolve().is_relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        fail(f"cannot resolve skill selector {selector!r}: {exc}")
    if not contained:
        fail(f"skill selector {selector!r} is outside source {source.name}: {root}")
    return target


def require_unique_link_names(links: list[tuple[str, Path]]) -> None:
    seen: dict[str, Path] = {}
    for name, target in links:
        existing = seen.get(name)
        if existing is not None:
            fail(
                f"duplicate discovered skill name {name!r}: {existing} and {target}; "
                "use include with a more specific path prefix"
            )
        seen[name] = target


def format_selector_choices(source: Source, matches: list[tuple[str, Path]]) -> str:
    return ", ".join(source_relative_selector(source, path) for _name, path in matches)


def workspace_source(workspace_root: Path) -> Source:
    return Source(name="workspace", path=workspace_root, remote=None)


def discover_skill_links(
    source: Source,
    *,
    prefix: str | None = None,
    skip_roots: set[Path] | None = None,
    scan: SkillScan | None = None,
    progress: Progress | None = None,
    warning_context: str = "",
) -> list[tuple[str, Path]]:
    scan = scan or SkillScan(source, skip_roots)
    if prefix is None:
        matches = [(path.name, path) for path in scan.active]
        if not matches:
            if scan.archived:
                fail(
                    f"source {source.name} contains only deprecated skills; select an exact skill path"
                )
            fail(f"no SKILL.md files found in source {source.name}: {source.path}")
        return matches

    target = validate_skill_selector(source, prefix)
    if not target.exists():
        fail(f"skill prefix for source {source.name} does not exist: {target}")
    if not scan.contains(target):
        fail(
            f"skill selector {prefix!r} is outside source {source.name}'s discovery scope"
        )
    marker = target / "SKILL.md"
    if marker.is_file():
        if not scan.contains(marker):
            fail(
                f"SKILL.md for selector {prefix!r} is outside source {source.name}'s discovery scope"
            )
        matches = [(target.name, target)]
        if is_archived_location(target):
            emit_event(
                progress,
                f"Warning: {warning_context}{deprecated_skill_message(source, target)}",
                severity="warning",
            )
    else:
        root = target.resolve()
        if is_archived_path(target) or is_archived_path(root):
            fail(f"deprecated directory {target}: select an exact skill path")
        matches = [(path.name, path) for path in scan.paths_under(target)]
        if not matches:
            fail(
                f"no SKILL.md files found in source {source.name} under prefix {prefix!r}: {source.path}"
            )
    return matches


def resolve_selector(
    source: Source,
    selector: str,
    *,
    skip_roots: set[Path] | None = None,
    allow_name_conflicts: bool = False,
    scan: SkillScan | None = None,
    progress: Progress | None = None,
    warning_context: str = "",
) -> list[tuple[str, Path]]:
    target = validate_skill_selector(source, selector)
    scan = scan or SkillScan(source, skip_roots)
    if selector == "*":
        matches = discover_skill_links(source, scan=scan)
        if not allow_name_conflicts:
            require_unique_link_names(matches)
        return matches
    if target.exists():
        matches = discover_skill_links(
            source,
            prefix=selector,
            scan=scan,
            progress=progress,
            warning_context=warning_context,
        )
    else:
        matches = [(path.name, path) for path in scan.active if path.name == selector]
        if not matches:
            archived = [
                path for path in scan.archived if path.name == Path(selector).name
            ]
            hint = ""
            if archived:
                paths = ", ".join(
                    source_relative_selector(source, path) for path in archived
                )
                hint = f"; deprecated candidates require an exact path: {paths}"
            fail(
                f"skill selector {selector!r} for source {source.name} matched no candidates{hint}"
            )

    if len(matches) > 1 and not allow_name_conflicts:
        fail(
            f"skill selector {selector!r} for source {source.name} matched multiple candidates. "
            f"Use a more specific selector: {format_selector_choices(source, matches)}"
        )
    return matches


def resolve_link_name_conflicts(
    links: list[SkillLinkCandidate],
    conflict_ui: SkillConflictUI | None,
    *,
    preview_conflicts: bool = False,
    progress: Progress | None = None,
) -> list[SkillLinkCandidate]:
    candidates_by_name: dict[str, list[SkillLinkCandidate]] = {}
    resolved_by_name: dict[str, set[Path]] = {}
    for link in links:
        resolved = link.target.resolve()
        if resolved in resolved_by_name.setdefault(link.name, set()):
            continue
        resolved_by_name[link.name].add(resolved)
        candidates_by_name.setdefault(link.name, []).append(link)

    selected: list[SkillLinkCandidate] = []
    for name, candidates in candidates_by_name.items():
        if len(candidates) == 1:
            selected.append(candidates[0])
            continue

        if preview_conflicts:
            emit_event(
                progress,
                f"conflict {name!r}: choose one source when running interactively",
            )
            for candidate in candidates:
                emit_event(
                    progress,
                    f"  candidate {candidate.source_name}: {candidate.target}",
                )
            continue
        if conflict_ui is None or not conflict_ui.is_interactive():
            choices = " and ".join(
                f"{candidate.source_name}: {candidate.target}"
                for candidate in candidates
            )
            raise SkillNameConflictError(
                f"duplicate discovered skill name {name!r}: {choices}"
            )

        try:
            choice = conflict_ui.select(name, tuple(candidates))
        except (OSError, RuntimeError) as exc:
            fail(f"interactive skill source selection failed for {name!r}: {exc}")
        if choice is None:
            fail(f"skill source selection cancelled for {name!r}")
        matching = next(
            (
                candidate
                for candidate in candidates
                if candidate.target.resolve() == choice.resolve()
            ),
            None,
        )
        if matching is None:
            fail(f"invalid skill source selected for {name!r}: {choice}")
        selected.append(matching)

    return selected


def is_windows_platform() -> bool:
    return os.name == "nt"


def default_link_mode() -> str:
    return "junction" if is_windows_platform() else "symlink"


def is_junction(path: Path) -> bool:
    if hasattr(path, "is_junction"):
        return path.is_junction()
    if os.name != "nt":
        return False
    try:
        return path.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (FileNotFoundError, NotADirectoryError):
        return False


def junction_failure_message(link: Path, target: Path, error: BaseException) -> str:
    return f"could not create junction {link} -> {target}: {error}"


def create_windows_junction(link: Path, target: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HAGENCY_PROFILE_JUNCTION_LINK": str(link),
            "HAGENCY_PROFILE_JUNCTION_TARGET": str(target),
        }
    )
    command = (
        "New-Item -ItemType Junction "
        "-Path $env:HAGENCY_PROFILE_JUNCTION_LINK "
        "-Target $env:HAGENCY_PROFILE_JUNCTION_TARGET | Out-Null"
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )


def validate_skills_dir(skills_dir: Path) -> None:
    for candidate in (skills_dir, *skills_dir.parents):
        if not candidate.exists() and os.path.lexists(candidate):
            kind = "junction" if is_junction(candidate) else "symlink"
            fail(f"skills destination is a broken {kind}: {candidate}")
        if candidate.exists():
            if not candidate.is_dir():
                fail(f"skills destination is not a directory: {candidate}")
            return


def _create_skills_dir(skills_dir: Path) -> None:
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"could not create skills destination {skills_dir}: {exc}")


def install_skill(
    skills_dir: Path,
    name: str,
    target: Path,
    *,
    link_mode: str,
    dry_run: bool,
    progress: Progress | None = None,
) -> None:
    validate_skills_dir(skills_dir)
    if not target.exists() and not dry_run:
        fail(f"link target does not exist: {target}")
    real_target = target.resolve() if target.exists() else target.absolute()
    link = skills_dir / name

    if link_mode == "copy":
        if os.path.lexists(link):
            fail(f"refusing to overwrite existing skill destination: {link}")
        emit_event(progress, f"copy {real_target} -> {link}")
        if not dry_run:
            if not real_target.is_dir():
                fail(f"copy target is not a directory: {real_target}")
            _create_skills_dir(skills_dir)
            shutil.copytree(real_target, link, symlinks=False)
        return

    if link_mode == "junction":
        if not is_windows_platform():
            fail("skill link mode junction is only supported on Windows")
        if not dry_run and not real_target.is_dir():
            fail(f"junction target is not a directory: {real_target}")
        if is_junction(link):
            existing = link.resolve()
            if existing == real_target:
                emit_event(progress, f"ok {link} -> {real_target}")
                return
            emit_event(progress, f"remove {link}")
            if not dry_run:
                link.rmdir()
        elif link.is_symlink():
            emit_event(progress, f"remove {link}")
            if not dry_run:
                link.unlink()
        elif link.exists():
            fail(f"refusing to overwrite non-junction: {link}")

        emit_event(progress, f"junction {link} -> {real_target}")
        if not dry_run:
            _create_skills_dir(skills_dir)
            try:
                create_windows_junction(link, real_target)
            except (OSError, subprocess.CalledProcessError) as exc:
                fail(junction_failure_message(link, real_target, exc))
        return

    if link_mode != "symlink":
        fail(f"unsupported skill link mode: {link_mode}")

    if link.is_symlink():
        existing = link.resolve()
        if existing == real_target:
            emit_event(progress, f"ok {link} -> {real_target}")
            return
        emit_event(progress, f"remove {link}")
        if not dry_run:
            link.unlink()
    elif link.exists():
        fail(f"refusing to overwrite non-symlink: {link}")

    emit_event(progress, f"link {link} -> {real_target}")
    if not dry_run:
        _create_skills_dir(skills_dir)
        try:
            os.symlink(real_target, link, target_is_directory=real_target.is_dir())
        except OSError as exc:
            raise SkillSymlinkError(
                f"could not create symlink {link} -> {real_target}: {exc}",
                windows=is_windows_platform(),
            ) from exc


def skill_source(
    source_name: str, sources: dict[str, Source], workspace: Source
) -> Source:
    if source_name == "workspace":
        return workspace
    source = sources.get(source_name)
    if source is None:
        fail(f"skill references unknown source: {source_name}")
    return source


DEFAULT_SKILLS_DIRECTORY = Path(".agents") / "skills"


LinkMode = Literal["symlink", "copy", "junction"]


def resolve_skill_install_dir(
    skills_path: str | None,
    skills_root: str | None,
    global_install: bool,
    cwd: Path,
    *,
    default_root: Path | None,
) -> Path:
    if sum((skills_path is not None, skills_root is not None, global_install)) > 1:
        fail("skill destinations --path, --dir, and --global are mutually exclusive")
    if skills_path is not None:
        return expand_path(skills_path, cwd)

    if skills_root is not None:
        install_root = expand_path(skills_root, cwd)
    elif global_install:
        install_root = Path.home()
    elif default_root is not None:
        install_root = default_root
    else:
        fail("skill destination requires --path or --dir")

    return install_root / DEFAULT_SKILLS_DIRECTORY


def resolve_link_mode(copy: bool, link_mode: LinkMode | None) -> str:
    if copy and link_mode in {"symlink", "junction"}:
        fail(f"-cp cannot be combined with --link-mode {link_mode}")
    if copy:
        return "copy"
    return link_mode or default_link_mode()


def skill_skip_roots(source_name: str, sources: dict) -> set[Path] | None:
    if source_name == "workspace":
        return {source.path for source in sources.values()}
    return None


def workspace_skill_scans(
    root: Path, sources: dict[str, Source]
) -> dict[str, SkillScan]:
    return {
        name: SkillScan(source, skill_skip_roots(name, sources))
        for name, source in {"workspace": workspace_source(root), **sources}.items()
    }
