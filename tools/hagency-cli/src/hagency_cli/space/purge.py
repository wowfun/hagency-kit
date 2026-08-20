from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

MIN_AGE_SECONDS = 7 * 24 * 60 * 60
MIN_SCAN_DEPTH = 1
MAX_SCAN_DEPTH = 6
CACHEDIR_TAG_NAME = "CACHEDIR.TAG"
CACHEDIR_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"

PURGE_TARGETS = frozenset(
    {
        "node_modules",
        "target",
        "build",
        "dist",
        "venv",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".ruff_cache",
        ".gradle",
        ".terragrunt-cache",
        "__pycache__",
        ".next",
        ".nuxt",
        ".output",
        "vendor",
        "bin",
        "obj",
        ".turbo",
        ".parcel-cache",
        ".dart_tool",
        ".zig-cache",
        "zig-out",
        ".angular",
        ".svelte-kit",
        ".astro",
        "coverage",
        "DerivedData",
        "Pods",
        ".cxx",
        ".expo",
        ".build",
    }
)

MONOREPO_INDICATORS = (
    ".git",
    "lerna.json",
    "pnpm-workspace.yaml",
    "nx.json",
    "rush.json",
)

PROJECT_INDICATORS = (
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "build.gradle",
    "terragrunt.hcl",
    "Gemfile",
    "composer.json",
    "pubspec.yaml",
    "Package.swift",
    "Makefile",
    "build.zig",
    "build.zig.zon",
    ".git",
)

DEFAULT_ROOT_NAMES = (
    "www",
    "dev",
    "Projects",
    "GitHub",
    "Code",
    "Workspace",
    "Repos",
    "Development",
)

EXPLICIT_HIDDEN_ROOTS = (
    Path(".codex") / "worktrees",
    Path(".claude") / "worktrees",
)

EXCLUDED_HOME_CHILDREN = frozenset(
    {
        "Applications",
        "AppData",
        "Box",
        "Desktop",
        "Documents",
        "Downloads",
        "Library",
        "Movies",
        "Music",
        "Pictures",
        "Public",
        "Dropbox",
        "Google Drive",
        "iCloud Drive",
    }
)

CLOUD_HOME_CHILD_PREFIXES = (
    "box",
    "creative cloud files",
    "dropbox",
    "google drive",
    "icloud",
    "nextcloud",
    "onedrive",
    "owncloud",
)

SCAN_PRUNE_NAMES = frozenset(
    {".git", ".hg", ".svn", ".Trash", "Applications", "AppData", "Library"}
)
SCAN_PRUNE_NAMES_CASEFOLD = frozenset(name.casefold() for name in SCAN_PRUNE_NAMES)


class Activity(str, Enum):
    OLD = "old"
    RECENT = "recent"
    UNCERTAIN = "uncertain"


class PurgeDisposition(str, Enum):
    PREVIEW = "preview"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    PARTIAL = "partial"


class ItemDisposition(str, Enum):
    WOULD_REMOVE = "would_remove"
    REMOVED = "removed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class PurgeRequest:
    paths: tuple[Path, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class PurgeChoice:
    id: str
    exact_path: Path
    project_path: Path
    artifact_kind: str
    size_bytes: int | None
    activity: Activity
    preselected: bool

    @property
    def label(self) -> str:
        size = (
            _format_bytes(self.size_bytes) if self.size_bytes is not None else "unknown"
        )
        return (
            f"{self.project_path.name} | {self.artifact_kind} | {size} | "
            f"{self.activity.value} | {self.exact_path}"
        )


class PurgeUI(Protocol):
    def is_interactive(self) -> bool: ...

    def select(self, choices: tuple[PurgeChoice, ...]) -> tuple[str, ...] | None: ...

    def confirm_exact(self, paths: tuple[Path, ...], known_bytes: int) -> bool: ...


@dataclass(frozen=True)
class PurgeIssue:
    code: str
    path: Path | None
    message: str
    is_failure: bool = True


@dataclass(frozen=True)
class PurgeItemResult:
    exact_path: Path
    disposition: ItemDisposition
    size_bytes: int | None
    message: str = ""


@dataclass(frozen=True)
class PurgeReport:
    disposition: PurgeDisposition
    roots: tuple[Path, ...]
    choices: tuple[PurgeChoice, ...]
    selected_paths: tuple[Path, ...]
    results: tuple[PurgeItemResult, ...]
    issues: tuple[PurgeIssue, ...]
    known_bytes: int = 0

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues) or any(
            result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
            for result in self.results
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class PathsEditReport:
    config_path: Path
    before_roots: tuple[Path, ...]
    after_roots: tuple[Path, ...]
    editor: str
    issues: tuple[PurgeIssue, ...]

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _GitContext:
    root: Path
    marker_identity: _Identity


@dataclass(frozen=True)
class _HardlinkEntry:
    identity: tuple[int, int] | None
    size_bytes: int


@dataclass(frozen=True)
class _PlannedCandidate:
    choice: PurgeChoice
    root: Path
    root_identity: _Identity
    parent_identity: _Identity
    target_identity: _Identity
    git_contexts: tuple[_GitContext, ...]
    hardlink_entries: tuple[_HardlinkEntry, ...]


@dataclass(frozen=True)
class _PurgePlan:
    roots: tuple[Path, ...]
    candidates: tuple[_PlannedCandidate, ...]
    issues: tuple[PurgeIssue, ...]


@dataclass(frozen=True)
class _ConfiguredPaths:
    paths: tuple[Path, ...]
    has_entries: bool
    issues: tuple[PurgeIssue, ...]


@dataclass(frozen=True)
class _DiscoveredRoots:
    paths: tuple[Path, ...]
    issues: tuple[PurgeIssue, ...]


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _identity(path: Path) -> _Identity:
    info = path.stat(follow_symlinks=False)
    if not info.st_ino:
        raise OSError(f"stable filesystem identity is unavailable for {path}")
    return _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _is_reparse_stat(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_link_or_reparse(path: Path) -> bool:
    info = path.stat(follow_symlinks=False)
    return stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info)


def _has_link_or_reparse_component(path: Path) -> bool:
    absolute = path if path.is_absolute() else (Path.cwd() / path)
    current = Path(absolute.anchor)
    start = 1 if absolute.anchor else 0
    for part in absolute.parts[start:]:
        current /= part
        if _is_link_or_reparse(current):
            return True
    return False


def _same_identity(path: Path, expected: _Identity) -> bool:
    try:
        return _identity(path) == expected
    except OSError:
        return False


def _canonical_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _is_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return allow_equal or bool(relative.parts)


def _expand_config_path(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith(("~/", "~\\")):
        return home / value[2:]
    return Path(value)


def purge_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    filename = "space-purge-paths"

    if platform == "win32":
        appdata = environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Hagency" / filename
    elif platform == "darwin":
        return home / "Library" / "Application Support" / "Hagency" / filename
    else:
        xdg_config_home = environ.get("XDG_CONFIG_HOME")
        if xdg_config_home and Path(xdg_config_home).is_absolute():
            return Path(xdg_config_home) / "hagency" / filename

    return home / ".config" / "hagency" / filename


def _read_configured_paths(config_path: Path, home: Path) -> _ConfiguredPaths:
    try:
        info = config_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _ConfiguredPaths((), False, ())
    except OSError as exc:
        return _ConfiguredPaths(
            (),
            True,
            (
                PurgeIssue(
                    "config_read_failed",
                    config_path,
                    f"could not inspect path config: {exc}",
                ),
            ),
        )
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse_stat(info)
    ):
        return _ConfiguredPaths(
            (),
            True,
            (
                PurgeIssue(
                    "config_read_failed",
                    config_path,
                    "path config must be a regular file, not a link or reparse point",
                ),
            ),
        )
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return _ConfiguredPaths(
            (),
            True,
            (
                PurgeIssue(
                    "config_read_failed",
                    config_path,
                    f"could not read path config: {exc}",
                ),
            ),
        )

    paths: list[Path] = []
    issues: list[PurgeIssue] = []
    has_entries = False
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        has_entries = True
        path = _expand_config_path(line, home)
        if not path.is_absolute():
            issues.append(
                PurgeIssue(
                    "config_path_not_absolute",
                    config_path,
                    f"line {number} must be an absolute or ~ path: {line}",
                )
            )
            continue
        paths.append(path)
    return _ConfiguredPaths(tuple(paths), has_entries, tuple(issues))


def _has_project_marker(directory: Path) -> bool:
    return any((directory / marker).exists() for marker in PROJECT_INDICATORS)


def _project_marker_state(directory: Path) -> tuple[bool, list[PurgeIssue]]:
    issues: list[PurgeIssue] = []
    for marker in PROJECT_INDICATORS:
        marker_path = directory / marker
        try:
            marker_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(
                PurgeIssue(
                    "discovery_stat_failed",
                    marker_path,
                    f"could not inspect project marker: {exc}",
                )
            )
        else:
            return True, issues
    return False, issues


def _contains_project_marker(
    root: Path, max_depth: int = 2
) -> tuple[bool, list[PurgeIssue]]:
    issues: list[PurgeIssue] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        has_marker, marker_issues = _project_marker_state(directory)
        issues.extend(marker_issues)
        if has_marker:
            return True, issues
        if depth >= max_depth:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if (
                        entry.name.startswith(".")
                        or entry.name.casefold() in SCAN_PRUNE_NAMES_CASEFOLD
                    ):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False) and not _is_reparse_stat(
                            entry.stat(follow_symlinks=False)
                        ):
                            stack.append((Path(entry.path), depth + 1))
                    except OSError as exc:
                        issues.append(
                            PurgeIssue(
                                "discovery_stat_failed",
                                Path(entry.path),
                                f"could not inspect potential project directory: {exc}",
                            )
                        )
                        continue
        except OSError as exc:
            issues.append(
                PurgeIssue(
                    "discovery_scan_failed",
                    directory,
                    f"could not scan potential project container: {exc}",
                )
            )
    return False, issues


def _autodiscover_roots(
    home: Path, *, environ: Mapping[str, str] | None = None
) -> _DiscoveredRoots:
    environ = os.environ if environ is None else environ
    candidates = [home / name for name in DEFAULT_ROOT_NAMES]
    candidates.extend(home / relative for relative in EXPLICIT_HIDDEN_ROOTS)
    issues: list[PurgeIssue] = []
    excluded_names = {name.casefold() for name in EXCLUDED_HOME_CHILDREN}
    explicit_cloud_roots = {
        _canonical_key(Path(value).expanduser())
        for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
        if (value := environ.get(key))
    }
    try:
        with os.scandir(home) as entries:
            for entry in entries:
                folded_name = entry.name.casefold()
                if (
                    entry.name.startswith(".")
                    or folded_name in excluded_names
                    or folded_name.startswith(CLOUD_HOME_CHILD_PREFIXES)
                ):
                    continue
                path = Path(entry.path)
                if _canonical_key(path) in explicit_cloud_roots:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if _is_reparse_stat(entry.stat(follow_symlinks=False)):
                        continue
                    if os.path.ismount(path):
                        continue
                except OSError as exc:
                    issues.append(
                        PurgeIssue(
                            "discovery_stat_failed",
                            path,
                            f"could not inspect home directory entry: {exc}",
                        )
                    )
                    continue
                contains_marker, marker_issues = _contains_project_marker(path)
                issues.extend(marker_issues)
                if contains_marker:
                    candidates.append(path)
    except OSError as exc:
        return _DiscoveredRoots(
            (),
            (
                PurgeIssue(
                    "discovery_scan_failed",
                    home,
                    f"could not scan home directory: {exc}",
                ),
            ),
        )

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or _is_reparse_stat(info):
                continue
            resolved = path.resolve()
            key = _canonical_key(resolved)
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(
                PurgeIssue(
                    "discovery_stat_failed",
                    path,
                    f"could not inspect automatic purge root: {exc}",
                )
            )
            continue
        if key not in seen:
            seen.add(key)
            deduped.append(resolved)
    return _DiscoveredRoots(tuple(deduped), tuple(issues))


def _validate_roots(
    raw_paths: tuple[Path, ...], home: Path
) -> tuple[tuple[Path, ...], tuple[PurgeIssue, ...]]:
    roots: list[Path] = []
    issues: list[PurgeIssue] = []
    seen: set[tuple[int, int] | str] = set()
    resolved_home = home.resolve()

    for raw_path in raw_paths:
        path = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path)
        try:
            if any(
                ord(character) < 32 or ord(character) == 127 for character in str(path)
            ):
                raise ValueError("control characters are not allowed in purge roots")
            if _has_link_or_reparse_component(path):
                raise ValueError("symlink, junction, or reparse roots are not allowed")
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("not a directory")
            if resolved.parent == resolved or resolved == resolved_home:
                raise ValueError("filesystem root and home directory are protected")
            identity = _identity(resolved)
            key: tuple[int, int] | str = (
                (identity.device, identity.inode)
                if identity.inode
                else _canonical_key(resolved)
            )
        except (OSError, ValueError) as exc:
            issues.append(
                PurgeIssue("invalid_root", path, f"invalid purge root: {exc}")
            )
            continue
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return tuple(roots), tuple(issues)


def _resolve_roots(
    request: PurgeRequest,
) -> tuple[tuple[Path, ...], tuple[PurgeIssue, ...]]:
    home = Path.home()
    if request.paths:
        return _validate_roots(request.paths, home)

    configured = _read_configured_paths(purge_config_path(home=home), home)
    discovered = (
        _DiscoveredRoots(configured.paths, ())
        if configured.has_entries
        else _autodiscover_roots(home)
    )
    raw_paths = discovered.paths
    roots, validation_issues = _validate_roots(raw_paths, home)
    return roots, (*configured.issues, *discovered.issues, *validation_issues)


def _valid_cachedir_tag(directory: Path) -> bool:
    tag = directory / CACHEDIR_TAG_NAME
    try:
        info = tag.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or _is_reparse_stat(info):
            return False
        with tag.open("rb") as handle:
            return handle.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False


def _find_project_root(candidate: Path, scan_root: Path) -> Path | None:
    current = candidate.parent
    project_root: Path | None = None
    while _is_within(current, scan_root, allow_equal=True):
        if any((current / marker).exists() for marker in MONOREPO_INDICATORS):
            return current
        if project_root is None and _has_project_marker(current):
            project_root = current
        if current == scan_root:
            break
        current = current.parent
    return project_root


def _is_dotnet_bin(path: Path) -> bool:
    parent = path.parent
    try:
        has_project = any(
            child.is_file()
            and child.suffix.lower() in {".csproj", ".fsproj", ".vbproj"}
            for child in parent.iterdir()
        )
    except OSError:
        return False
    return has_project and ((path / "Debug").is_dir() or (path / "Release").is_dir())


def _context_allows(path: Path, project_root: Path) -> bool:
    if path.name == "vendor" and not (path.parent / "composer.json").is_file():
        return False
    if path.name == "bin" and not _is_dotnet_bin(path):
        return False
    if path.name == "DerivedData":
        parts = path.parts
        for index in range(len(parts) - 3):
            if parts[index : index + 4] == (
                "Library",
                "Developer",
                "Xcode",
                "DerivedData",
            ):
                return False
    return _is_within(path, project_root)


def _git_context_from_marker(marker: Path) -> tuple[_GitContext | None, str | None]:
    try:
        info = marker.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not inspect {marker}: {exc}"
    if (
        stat.S_ISLNK(info.st_mode)
        or _is_reparse_stat(info)
        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
    ):
        return None, f"unsafe Git marker: {marker}"
    if not info.st_ino:
        return None, f"stable Git marker identity is unavailable: {marker}"
    return (
        _GitContext(
            root=marker.parent.resolve(),
            marker_identity=_Identity(
                info.st_dev,
                info.st_ino,
                stat.S_IFMT(info.st_mode),
            ),
        ),
        None,
    )


def _discover_git_contexts(
    path: Path,
) -> tuple[tuple[_GitContext, ...], str | None]:
    contexts: dict[str, _GitContext] = {}
    current = path
    while True:
        context, error = _git_context_from_marker(current / ".git")
        if error is not None:
            return (), error
        if context is not None:
            contexts[_canonical_key(context.root)] = context
        if current.parent == current:
            break
        current = current.parent

    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                entry_list = list(entries)
        except OSError as exc:
            return (
                (),
                f"could not inspect nested Git repositories under {directory}: {exc}",
            )
        for entry in entry_list:
            entry_path = Path(entry.path)
            if entry.name.casefold() == ".git":
                context, error = _git_context_from_marker(entry_path)
                if error is not None:
                    return (), error
                if context is not None:
                    contexts[_canonical_key(context.root)] = context
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return (
                    (),
                    f"could not inspect {entry_path} for nested Git repositories: {exc}",
                )
            if (
                stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and not _is_reparse_stat(info)
            ):
                stack.append(entry_path)

    return tuple(contexts[key] for key in sorted(contexts)), None


def _git_tracked_state(path: Path, git_root: Path) -> tuple[bool | None, str | None]:
    try:
        relative = path.relative_to(git_root).as_posix()
        git_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_environment["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(git_root),
                "ls-files",
                "-z",
                "--",
                relative,
            ],
            capture_output=True,
            check=False,
            env=git_environment,
            timeout=10,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        return None, detail or f"git exited with {result.returncode}"
    return bool(result.stdout), None


def _measure_candidate(
    path: Path,
    now: float,
) -> tuple[int | None, Activity, str | None, tuple[_HardlinkEntry, ...]]:
    seen: set[tuple[int, int]] = set()
    hardlink_entries: list[_HardlinkEntry] = []
    total = 0
    newest = 0.0
    stack = [path]
    try:
        while stack:
            current = stack.pop()
            info = current.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info):
                newest = max(newest, info.st_mtime)
                continue
            if stat.S_ISDIR(info.st_mode) and os.path.ismount(current):
                return (
                    None,
                    Activity.UNCERTAIN,
                    f"refusing to measure mount point at {current}",
                    (),
                )
            identity = (info.st_dev, info.st_ino)
            if not info.st_ino or identity not in seen:
                if info.st_ino:
                    seen.add(identity)
                blocks = getattr(info, "st_blocks", None)
                allocated = (
                    blocks * 512
                    if os.name != "nt" and blocks is not None
                    else info.st_size
                )
                total += allocated
                if info.st_ino and info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
                    hardlink_entries.append(_HardlinkEntry(identity, allocated))
            newest = max(newest, info.st_mtime)
            if not stat.S_ISDIR(info.st_mode):
                continue
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    except OSError as exc:
        return (
            None,
            Activity.UNCERTAIN,
            f"could not measure {current}: {exc}",
            (),
        )

    activity = Activity.OLD if newest < now - MIN_AGE_SECONDS else Activity.RECENT
    return total, activity, None, tuple(hardlink_entries)


def _candidate_id(path: Path, identity: _Identity) -> str:
    payload = f"{path}\0{identity.device}\0{identity.inode}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _scan_root(
    root: Path,
    now: float,
) -> tuple[list[_PlannedCandidate], list[PurgeIssue]]:
    candidates: list[_PlannedCandidate] = []
    issues: list[PurgeIssue] = []
    try:
        root_identity = _identity(root)
    except OSError as exc:
        return [], [
            PurgeIssue("root_stat_failed", root, f"could not inspect root: {exc}")
        ]

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                entry_list = list(entries)
        except OSError as exc:
            issues.append(
                PurgeIssue("scan_failed", directory, f"could not scan directory: {exc}")
            )
            continue

        for entry in entry_list:
            path = Path(entry.path)
            child_depth = depth + 1
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                issues.append(
                    PurgeIssue(
                        "scan_stat_failed", path, f"could not inspect path: {exc}"
                    )
                )
                continue
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _is_reparse_stat(info)
            ):
                continue
            if entry.name.casefold() in SCAN_PRUNE_NAMES_CASEFOLD:
                continue

            named_target = entry.name in PURGE_TARGETS
            tagged_target = _valid_cachedir_tag(path)
            if child_depth >= MIN_SCAN_DEPTH and (named_target or tagged_target):
                project_root = _find_project_root(path, root)
                if project_root is not None and _context_allows(path, project_root):
                    git_contexts, git_error = _discover_git_contexts(path)
                    if git_error is not None:
                        issues.append(
                            PurgeIssue(
                                "git_check_failed",
                                path,
                                f"could not prove candidate is untracked: {git_error}",
                            )
                        )
                    else:
                        tracked: bool | None = False
                        for git_context in git_contexts:
                            tracked_path = (
                                path
                                if _is_within(
                                    path,
                                    git_context.root,
                                    allow_equal=True,
                                )
                                else git_context.root
                            )
                            tracked, git_error = _git_tracked_state(
                                tracked_path, git_context.root
                            )
                            if tracked is None or tracked:
                                break
                        if tracked is None:
                            issues.append(
                                PurgeIssue(
                                    "git_check_failed",
                                    path,
                                    "could not prove candidate is untracked: "
                                    f"{git_error}",
                                )
                            )
                            continue
                        if tracked:
                            continue
                        try:
                            parent_identity = _identity(path.parent)
                            target_identity = _identity(path)
                        except OSError as exc:
                            issues.append(
                                PurgeIssue(
                                    "candidate_stat_failed",
                                    path,
                                    f"could not bind candidate identity: {exc}",
                                )
                            )
                        else:
                            (
                                size_bytes,
                                activity,
                                measure_error,
                                hardlink_entries,
                            ) = _measure_candidate(path, now)
                            if measure_error is not None:
                                issues.append(
                                    PurgeIssue(
                                        "candidate_measure_failed",
                                        path,
                                        measure_error,
                                    )
                                )
                            if size_bytes != 0:
                                choice = PurgeChoice(
                                    id=_candidate_id(path, target_identity),
                                    exact_path=path.resolve(),
                                    project_path=project_root.resolve(),
                                    artifact_kind=entry.name
                                    if named_target
                                    else CACHEDIR_TAG_NAME,
                                    size_bytes=size_bytes,
                                    activity=activity,
                                    preselected=activity is Activity.OLD
                                    and size_bytes is not None,
                                )
                                candidates.append(
                                    _PlannedCandidate(
                                        choice=choice,
                                        root=root,
                                        root_identity=root_identity,
                                        parent_identity=parent_identity,
                                        target_identity=target_identity,
                                        git_contexts=git_contexts,
                                        hardlink_entries=hardlink_entries,
                                    )
                                )
                # Never descend into a known artifact tree, even when protected.
                continue

            if child_depth < MAX_SCAN_DEPTH:
                stack.append((path, child_depth))
    if not _same_identity(root, root_identity):
        return [], [
            *issues,
            PurgeIssue(
                "root_changed_during_scan",
                root,
                "scan root changed before its results could be published",
            ),
        ]
    stable_candidates: list[_PlannedCandidate] = []
    for candidate in candidates:
        if _same_identity(
            candidate.choice.exact_path.parent, candidate.parent_identity
        ) and _same_identity(candidate.choice.exact_path, candidate.target_identity):
            stable_candidates.append(candidate)
        else:
            issues.append(
                PurgeIssue(
                    "candidate_changed_during_scan",
                    candidate.choice.exact_path,
                    "candidate or its parent changed before scan results were published",
                )
            )
    return stable_candidates, issues


def _drop_duplicate_and_nested(
    candidates: list[_PlannedCandidate],
) -> list[_PlannedCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            len(item.choice.exact_path.parts),
            str(item.choice.exact_path),
        ),
    )
    kept: list[_PlannedCandidate] = []
    identities: set[tuple[int, int] | str] = set()
    for candidate in ordered:
        identity: tuple[int, int] | str = (
            (candidate.target_identity.device, candidate.target_identity.inode)
            if candidate.target_identity.inode
            else _canonical_key(candidate.choice.exact_path)
        )
        if identity in identities:
            continue
        if any(
            _is_within(candidate.choice.exact_path, item.choice.exact_path)
            for item in kept
        ):
            continue
        identities.add(identity)
        kept.append(candidate)
    return kept


def _sort_candidates(
    candidates: list[_PlannedCandidate],
) -> tuple[_PlannedCandidate, ...]:
    project_totals: dict[Path, int] = {}
    seen_hardlinks: set[tuple[int, int]] = set()
    for candidate in sorted(candidates, key=lambda item: str(item.choice.exact_path)):
        project = candidate.choice.project_path
        accounted_size = candidate.choice.size_bytes or 0
        for entry in candidate.hardlink_entries:
            if entry.identity is None:
                continue
            if entry.identity in seen_hardlinks:
                accounted_size -= entry.size_bytes
            else:
                seen_hardlinks.add(entry.identity)
        project_totals[project] = project_totals.get(project, 0) + max(
            accounted_size, 0
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                -project_totals[item.choice.project_path],
                str(item.choice.project_path),
                -(item.choice.size_bytes if item.choice.size_bytes is not None else -1),
                str(item.choice.exact_path),
            ),
        )
    )


def _build_plan(request: PurgeRequest) -> _PurgePlan:
    roots, root_issues = _resolve_roots(request)
    now = time.time()
    candidates: list[_PlannedCandidate] = []
    issues = list(root_issues)
    for root in roots:
        found, scan_issues = _scan_root(root, now)
        candidates.extend(found)
        issues.extend(scan_issues)
    candidates = _drop_duplicate_and_nested(candidates)
    return _PurgePlan(roots, _sort_candidates(candidates), tuple(issues))


def _partial_or(
    disposition: PurgeDisposition,
    issues: tuple[PurgeIssue, ...],
    results: tuple[PurgeItemResult, ...] = (),
) -> PurgeDisposition:
    if disposition in {PurgeDisposition.PREVIEW, PurgeDisposition.CANCELLED}:
        return disposition
    if any(issue.is_failure for issue in issues) or any(
        result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
        for result in results
    ):
        return PurgeDisposition.PARTIAL
    return disposition


def _selection_known_bytes(selected: tuple[_PlannedCandidate, ...]) -> int:
    seen: set[tuple[int, int]] = set()
    total = sum(
        candidate.choice.size_bytes or 0
        for candidate in selected
        if candidate.choice.size_bytes is not None
    )
    for candidate in selected:
        if candidate.choice.size_bytes is None:
            continue
        for entry in candidate.hardlink_entries:
            if entry.identity in seen:
                total -= entry.size_bytes
            else:
                seen.add(entry.identity)
    return total


def _make_report(
    plan: _PurgePlan,
    disposition: PurgeDisposition,
    *,
    selected: tuple[_PlannedCandidate, ...] = (),
    results: tuple[PurgeItemResult, ...] = (),
    extra_issues: tuple[PurgeIssue, ...] = (),
) -> PurgeReport:
    issues = (*plan.issues, *extra_issues)
    return PurgeReport(
        disposition=_partial_or(disposition, issues, results),
        roots=plan.roots,
        choices=tuple(item.choice for item in plan.candidates),
        selected_paths=tuple(item.choice.exact_path for item in selected),
        results=results,
        issues=issues,
        known_bytes=_selection_known_bytes(selected),
    )


def _bind_selection(
    ids: tuple[str, ...], plan: _PurgePlan
) -> tuple[tuple[_PlannedCandidate, ...], tuple[PurgeIssue, ...]]:
    by_id = {candidate.choice.id: candidate for candidate in plan.candidates}
    if len(ids) != len(set(ids)):
        return (), (
            PurgeIssue("invalid_selection", None, "selection contains duplicate IDs"),
        )
    unknown = [value for value in ids if value not in by_id]
    if unknown:
        return (), (
            PurgeIssue(
                "invalid_selection",
                None,
                f"selection contains unknown IDs: {', '.join(unknown)}",
            ),
        )
    return tuple(by_id[value] for value in ids), ()


def _revalidate(candidate: _PlannedCandidate) -> str | None:
    path = candidate.choice.exact_path
    if not _same_identity(candidate.root, candidate.root_identity):
        return "scan root changed after review"
    if not _same_identity(path.parent, candidate.parent_identity):
        return "parent directory changed after review"
    if not _same_identity(path, candidate.target_identity):
        return "candidate changed after review"
    try:
        if _is_link_or_reparse(path) or not _is_within(
            path.resolve(), candidate.root.resolve()
        ):
            return "candidate is no longer a safe real directory"
    except OSError:
        return "candidate can no longer be resolved safely"

    if candidate.choice.artifact_kind == CACHEDIR_TAG_NAME:
        if not _valid_cachedir_tag(path):
            return "candidate no longer has a valid CACHEDIR.TAG"
    elif path.name != candidate.choice.artifact_kind or path.name not in PURGE_TARGETS:
        return "candidate no longer matches the purge catalog"

    project_root = _find_project_root(path, candidate.root)
    if project_root is None or not _context_allows(path, project_root):
        return "candidate no longer has a safe project context"
    if project_root.resolve() != candidate.choice.project_path:
        return "candidate project ownership changed after review"

    git_contexts, git_error = _discover_git_contexts(path)
    if git_error is not None:
        return f"Git safety check failed: {git_error}"
    if git_contexts != candidate.git_contexts:
        return "candidate Git repository identity changed after review"
    for git_context in git_contexts:
        tracked_path = (
            path
            if _is_within(path, git_context.root, allow_equal=True)
            else git_context.root
        )
        tracked, git_error = _git_tracked_state(tracked_path, git_context.root)
        if tracked is None:
            return f"Git safety check failed: {git_error}"
        if tracked:
            return "candidate now contains Git-tracked content"

    _size, activity, measure_error, _entries = _measure_candidate(path, time.time())
    if measure_error is not None:
        return f"activity safety check failed: {measure_error}"
    if candidate.choice.activity is Activity.OLD and activity is not Activity.OLD:
        return "candidate activity changed after review"
    if (
        candidate.choice.activity is not Activity.UNCERTAIN
        and activity is Activity.UNCERTAIN
    ):
        return "candidate activity can no longer be verified"
    return None


def _remove_tree_no_follow(
    path: Path, expected_identity: _Identity | None = None
) -> None:
    stack: list[tuple[Path, _Identity | None, bool]] = [
        (path, expected_identity, False)
    ]
    while stack:
        current, expected, visited = stack.pop()
        info = current.stat(follow_symlinks=False)
        identity = _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        if (
            not identity.inode
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse_stat(info)
        ):
            raise OSError(f"refusing to recurse into unsafe directory: {current}")
        if expected is not None and identity != expected:
            raise OSError(f"directory identity changed during removal: {current}")
        if visited:
            current.rmdir()
            continue

        with os.scandir(current) as entries:
            children = [Path(entry.path) for entry in entries]
        stack.append((current, identity, True))
        for child in reversed(children):
            child_info = child.stat(follow_symlinks=False)
            child_identity = _Identity(
                child_info.st_dev,
                child_info.st_ino,
                stat.S_IFMT(child_info.st_mode),
            )
            if stat.S_ISDIR(child_info.st_mode) and not _is_reparse_stat(child_info):
                stack.append((child, child_identity, False))
            elif stat.S_ISDIR(child_info.st_mode) and _is_reparse_stat(child_info):
                child.rmdir()
            else:
                child.unlink()


@dataclass
class _FdRemovalFrame:
    directory_fd: int
    parent_fd: int | None
    name: str | None
    expected_identity: _Identity
    names: list[str]
    index: int = 0
    owns_fd: bool = True


def _remove_tree_from_fd(directory_fd: int) -> None:
    root_info = os.fstat(directory_fd)
    frames = [
        _FdRemovalFrame(
            directory_fd=directory_fd,
            parent_fd=None,
            name=None,
            expected_identity=_Identity(
                root_info.st_dev,
                root_info.st_ino,
                stat.S_IFMT(root_info.st_mode),
            ),
            names=os.listdir(directory_fd),
            owns_fd=False,
        )
    ]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        while frames:
            frame = frames[-1]
            if frame.index >= len(frame.names):
                try:
                    if frame.parent_fd is not None and frame.name is not None:
                        final_info = os.stat(
                            frame.name,
                            dir_fd=frame.parent_fd,
                            follow_symlinks=False,
                        )
                        final_identity = _Identity(
                            final_info.st_dev,
                            final_info.st_ino,
                            stat.S_IFMT(final_info.st_mode),
                        )
                        if final_identity != frame.expected_identity:
                            raise OSError(
                                "directory identity changed during removal: "
                                f"{frame.name}"
                            )
                        os.rmdir(frame.name, dir_fd=frame.parent_fd)
                finally:
                    if frame.owns_fd:
                        os.close(frame.directory_fd)
                        frame.owns_fd = False
                frames.pop()
                continue

            name = frame.names[frame.index]
            frame.index += 1
            info = os.stat(name, dir_fd=frame.directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not _is_reparse_stat(info):
                child_fd = os.open(name, flags, dir_fd=frame.directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    expected = _Identity(
                        info.st_dev,
                        info.st_ino,
                        stat.S_IFMT(info.st_mode),
                    )
                    actual = _Identity(
                        opened.st_dev,
                        opened.st_ino,
                        stat.S_IFMT(opened.st_mode),
                    )
                    if not actual.inode or actual != expected:
                        raise OSError(
                            f"directory identity changed during removal: {name}"
                        )
                    names = os.listdir(child_fd)
                except BaseException:
                    os.close(child_fd)
                    raise
                frames.append(
                    _FdRemovalFrame(
                        directory_fd=child_fd,
                        parent_fd=frame.directory_fd,
                        name=name,
                        expected_identity=expected,
                        names=names,
                    )
                )
            elif stat.S_ISDIR(info.st_mode) and _is_reparse_stat(info):
                os.rmdir(name, dir_fd=frame.directory_fd)
            else:
                os.unlink(name, dir_fd=frame.directory_fd)
    finally:
        for frame in frames:
            if frame.owns_fd:
                os.close(frame.directory_fd)
                frame.owns_fd = False


def _permanently_remove(candidate: _PlannedCandidate) -> None:
    path = candidate.choice.exact_path
    has_fd_removal = os.listdir in os.supports_fd and all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.unlink, os.rmdir)
    )
    if os.name == "nt" or not has_fd_removal:
        if not _same_identity(path.parent, candidate.parent_identity):
            raise OSError("parent identity changed immediately before removal")
        if not _same_identity(path, candidate.target_identity):
            raise OSError("candidate identity changed immediately before removal")
        quarantine = path.parent / f".hagency-purge-{uuid.uuid4().hex}"
        os.replace(path, quarantine)
        try:
            if not _same_identity(quarantine, candidate.target_identity):
                raise OSError("candidate identity changed before atomic quarantine")
            _remove_tree_no_follow(quarantine, candidate.target_identity)
        except BaseException as exc:
            recovery = ""
            if os.path.lexists(quarantine) and not os.path.lexists(path):
                try:
                    os.replace(quarantine, path)
                except OSError:
                    recovery = f"; remaining data is at {quarantine}"
            elif os.path.lexists(quarantine):
                recovery = f"; remaining data is at {quarantine}"
            if isinstance(exc, OSError):
                raise OSError(f"{exc}{recovery}") from exc
            if recovery and hasattr(exc, "add_note"):
                exc.add_note(recovery.removeprefix("; "))
            raise
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, flags)
    try:
        parent_info = os.fstat(parent_fd)
        if (
            _Identity(
                parent_info.st_dev,
                parent_info.st_ino,
                stat.S_IFMT(parent_info.st_mode),
            )
            != candidate.parent_identity
        ):
            raise OSError("parent identity changed immediately before removal")
        target_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _Identity(
                target_info.st_dev,
                target_info.st_ino,
                stat.S_IFMT(target_info.st_mode),
            )
            != candidate.target_identity
        ):
            raise OSError("candidate identity changed immediately before removal")
        if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
            raise OSError("candidate is no longer a real directory")

        target_fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(target_fd)
            if (opened.st_dev, opened.st_ino) != (
                target_info.st_dev,
                target_info.st_ino,
            ):
                raise OSError("candidate identity changed during removal")
            _remove_tree_from_fd(target_fd)
        finally:
            os.close(target_fd)
        final_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (final_info.st_dev, final_info.st_ino) != (
            target_info.st_dev,
            target_info.st_ino,
        ):
            raise OSError("candidate identity changed before final removal")
        os.rmdir(path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def purge_space(request: PurgeRequest, *, ui: PurgeUI) -> PurgeReport:
    plan = _build_plan(request)
    interactive = ui.is_interactive()

    if not interactive:
        selected = tuple(item for item in plan.candidates if item.choice.preselected)
        results = tuple(
            PurgeItemResult(
                item.choice.exact_path,
                ItemDisposition.WOULD_REMOVE,
                item.choice.size_bytes,
                "non-interactive preview",
            )
            for item in selected
        )
        return _make_report(
            plan, PurgeDisposition.PREVIEW, selected=selected, results=results
        )

    if not plan.candidates:
        return _make_report(plan, PurgeDisposition.COMPLETED)

    selected_ids = ui.select(tuple(item.choice for item in plan.candidates))
    if selected_ids is None or not selected_ids:
        return _make_report(plan, PurgeDisposition.CANCELLED)
    selected, selection_issues = _bind_selection(selected_ids, plan)
    if selection_issues:
        return _make_report(
            plan,
            PurgeDisposition.PARTIAL,
            extra_issues=selection_issues,
        )

    if request.dry_run:
        results = tuple(
            PurgeItemResult(
                item.choice.exact_path,
                ItemDisposition.WOULD_REMOVE,
                item.choice.size_bytes,
                "dry-run preview",
            )
            for item in selected
        )
        return _make_report(
            plan, PurgeDisposition.PREVIEW, selected=selected, results=results
        )

    known_bytes = _selection_known_bytes(selected)
    exact_paths = tuple(item.choice.exact_path for item in selected)
    if not ui.confirm_exact(exact_paths, known_bytes):
        return _make_report(plan, PurgeDisposition.CANCELLED, selected=selected)

    results: list[PurgeItemResult] = []
    for item in selected:
        reason = _revalidate(item)
        if reason is not None:
            results.append(
                PurgeItemResult(
                    item.choice.exact_path,
                    ItemDisposition.SKIPPED,
                    item.choice.size_bytes,
                    reason,
                )
            )
            continue
        try:
            _permanently_remove(item)
        except OSError as exc:
            results.append(
                PurgeItemResult(
                    item.choice.exact_path,
                    ItemDisposition.FAILED,
                    item.choice.size_bytes,
                    str(exc),
                )
            )
        else:
            results.append(
                PurgeItemResult(
                    item.choice.exact_path,
                    ItemDisposition.REMOVED,
                    item.choice.size_bytes,
                )
            )
    result_tuple = tuple(results)
    return _make_report(
        plan,
        PurgeDisposition.COMPLETED,
        selected=selected,
        results=result_tuple,
    )


def _write_config_template(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    template = (
        "# Hagency project artifact purge paths\n"
        "# Add one absolute or ~ path per line.\n"
        "# Leave this file empty to use automatic discovery.\n"
        "#\n"
        "# ~/Projects\n"
        "# ~/Work/ClientA\n"
    )
    try:
        with config_path.open("x", encoding="utf-8") as handle:
            handle.write(template)
    except FileExistsError:
        return


def _effective_config_roots(
    config_path: Path, home: Path
) -> tuple[tuple[Path, ...], tuple[PurgeIssue, ...]]:
    configured = _read_configured_paths(config_path, home)
    discovered = (
        _DiscoveredRoots(configured.paths, ())
        if configured.has_entries
        else _autodiscover_roots(home)
    )
    roots, issues = _validate_roots(discovered.paths, home)
    return roots, (*configured.issues, *discovered.issues, *issues)


def _split_editor_command(value: str) -> list[str]:
    command = shlex.split(value, posix=sys.platform != "win32")
    if sys.platform == "win32":
        command = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
            else token
            for token in command
        ]
    return command


def edit_purge_paths() -> PathsEditReport:
    home = Path.home()
    config_path = purge_config_path(home=home)
    before_roots, before_issues = _effective_config_roots(config_path, home)
    issues = list(before_issues)
    editor_value = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor_value:
        if sys.platform == "win32":
            editor_value = "notepad.exe"
        elif sys.platform == "darwin":
            editor_value = "open -W -t"
        else:
            editor_value = "vi"

    try:
        _write_config_template(config_path)
        editor_command = _split_editor_command(editor_value)
        if not editor_command:
            raise ValueError("editor command is empty")
        result = subprocess.run([*editor_command, str(config_path)], check=False)
        if result.returncode != 0:
            issues.append(
                PurgeIssue(
                    "editor_failed",
                    config_path,
                    f"editor exited with {result.returncode}",
                )
            )
    except (OSError, ValueError) as exc:
        issues.append(
            PurgeIssue(
                "editor_failed", config_path, f"could not edit path config: {exc}"
            )
        )

    after_roots, after_issues = _effective_config_roots(config_path, home)
    issues.extend(after_issues)
    return PathsEditReport(
        config_path=config_path,
        before_roots=before_roots,
        after_roots=after_roots,
        editor=editor_value,
        issues=tuple(issues),
    )


__all__ = [
    "Activity",
    "ItemDisposition",
    "PathsEditReport",
    "PurgeChoice",
    "PurgeDisposition",
    "PurgeIssue",
    "PurgeItemResult",
    "PurgeReport",
    "PurgeRequest",
    "PurgeUI",
    "edit_purge_paths",
    "purge_config_path",
    "purge_space",
]
