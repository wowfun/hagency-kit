from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from hagency_cli.files.cleanup.models import (
    CACHEDIR_TAG_NAME,
    CACHEDIR_TAG_SIGNATURE,
    MIN_AGE_SECONDS,
    MONOREPO_INDICATORS,
    PROJECT_INDICATORS,
    SCAN_PRUNE_NAMES_CASEFOLD,
    Activity,
    CleanupIssue,
    _GitContext,
    _HardlinkEntry,
    _Identity,
    _TreeEntry,
)

_DEADLINE: ContextVar[float | None] = ContextVar("cleanup_deadline", default=None)
_BUDGET_SECONDS: ContextVar[float] = ContextVar("cleanup_budget_seconds", default=60)
_CHECKPOINT_HOOK: ContextVar[Callable[[], None] | None] = ContextVar(
    "cleanup_checkpoint_hook", default=None
)
_MEASURE_HOOK: ContextVar[Callable[[Path, bool], None] | None] = ContextVar(
    "cleanup_measure_hook", default=None
)
_MOUNTS: ContextVar[frozenset[Path] | None] = ContextVar("cleanup_mounts", default=None)


def _mount_points() -> frozenset[Path]:
    if not sys.platform.startswith("linux"):
        return frozenset()
    points = set()
    with Path("/proc/self/mountinfo").open(
        encoding="utf-8", errors="surrogateescape"
    ) as stream:
        for line in stream:
            checkpoint()
            fields = line.split()
            if len(fields) < 10 or "-" not in fields:
                raise OSError("could not verify Linux mount table")
            decoded = re.sub(
                r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
            )
            points.add(Path(decoded))
    if not points:
        raise OSError("could not verify Linux mount table")
    return frozenset(points)


@contextmanager
def mount_check_scope() -> Iterator[None]:
    token = _MOUNTS.set(_mount_points())
    try:
        yield
    finally:
        _MOUNTS.reset(token)


def _is_mount(path: Path | str) -> bool:
    if os.path.ismount(path):
        return True
    # ismount() alone misses bind mounts on the same filesystem.
    points = _MOUNTS.get()
    if points is None:
        points = _mount_points()
    return Path(os.path.realpath(path)) in points


class ScanTimeout(OSError):
    """A scan budget expired; incomplete candidates must not be published."""


def checkpoint(deadline: float | None = None) -> None:
    hook = _CHECKPOINT_HOOK.get()
    if hook is not None:
        hook()
    deadline = _DEADLINE.get() if deadline is None else deadline
    if deadline is not None and time.monotonic() >= deadline:
        raise ScanTimeout(
            f"cleanup source scan exceeded {_BUDGET_SECONDS.get():g} seconds"
        )


@contextmanager
def scan_budget(
    seconds: float | None = 60, *, limit: float | None = None
) -> Iterator[None]:
    token = _DEADLINE.set(None if seconds is None else time.monotonic() + seconds)
    limit_token = _BUDGET_SECONDS.set(limit if limit is not None else seconds or 0)
    try:
        if _MOUNTS.get() is not None:
            yield
        else:
            with mount_check_scope():
                yield
    finally:
        _BUDGET_SECONDS.reset(limit_token)
        _DEADLINE.reset(token)


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
    return os.path.normcase(str(_resolve_path(path)))


def _resolve_path(path: Path, *, strict: bool = False) -> Path:
    try:
        return path.resolve(strict=strict)
    except RuntimeError as exc:
        # Python 3.11 reports symlink loops as RuntimeError instead of OSError.
        raise OSError(f"cannot resolve path {path}: {exc}") from exc


def _is_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    # Links must be resolved or rejected by the caller; normalize lexical '..'
    # before comparing containment so it cannot masquerade as a descendant.
    try:
        relative = Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
    except ValueError:
        return False
    return allow_equal or bool(relative.parts)


def _expand_config_path(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith(("~/", "~\\")):
        return home / value[2:]
    return Path(value)


def _has_project_marker(directory: Path) -> bool:
    return any((directory / marker).exists() for marker in PROJECT_INDICATORS)


def _project_marker_state(directory: Path) -> tuple[bool, list[CleanupIssue]]:
    issues: list[CleanupIssue] = []
    for marker in PROJECT_INDICATORS:
        marker_path = directory / marker
        try:
            marker_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(
                CleanupIssue(
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
) -> tuple[bool, list[CleanupIssue]]:
    issues: list[CleanupIssue] = []
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
                            CleanupIssue(
                                "discovery_stat_failed",
                                Path(entry.path),
                                f"could not inspect potential project directory: {exc}",
                            )
                        )
                        continue
        except OSError as exc:
            issues.append(
                CleanupIssue(
                    "discovery_scan_failed",
                    directory,
                    f"could not scan potential project container: {exc}",
                )
            )
    return False, issues


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


def _artifact_verification_reason(path: Path) -> str | None:
    if path.name == "vendor" and not (path.parent / "composer.json").is_file():
        return "vendor has no adjacent composer.json confirming Composer dependencies"
    if path.name == "bin" and not _is_dotnet_bin(path):
        return "bin lacks a .NET project with Debug or Release output"
    return None


def _context_protection_reason(path: Path, project_root: Path) -> str | None:
    if path.name == "DerivedData":
        parts = path.parts
        for index in range(len(parts) - 3):
            if parts[index : index + 4] == (
                "Library",
                "Developer",
                "Xcode",
                "DerivedData",
            ):
                return "global Xcode DerivedData is protected"
    if not _is_within(path, project_root):
        return "candidate is outside its project context"
    return None


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
            root=_resolve_path(marker.parent),
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
    current = path if path.is_dir() else path.parent
    while True:
        context, error = _git_context_from_marker(current / ".git")
        if error is not None:
            return (), error
        if context is not None:
            contexts[_canonical_key(context.root)] = context
        if current.parent == current:
            break
        current = current.parent

    return _discover_nested_git_contexts(path, tuple(contexts.values()))


def _discover_nested_git_contexts(
    path: Path, ancestors: tuple[_GitContext, ...]
) -> tuple[tuple[_GitContext, ...], str | None]:
    contexts = {_canonical_key(context.root): context for context in ancestors}
    stack = [path] if path.is_dir() else []
    while stack:
        checkpoint()
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
            checkpoint()
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
                if _is_mount(entry_path):
                    return (), f"mount point inside candidate: {entry_path}"
                stack.append(entry_path)

    return tuple(contexts[key] for key in sorted(contexts)), None


def git_query_timeout() -> float:
    """Use the remaining budget at process launch, without a post-deadline grace."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return 10
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScanTimeout(
            f"cleanup source scan exceeded {_BUDGET_SECONDS.get():g} seconds"
        )
    return min(10, remaining)


def _git_tracked_state(path: Path, git_root: Path) -> tuple[bool | None, str | None]:
    try:
        checkpoint()
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
            timeout=git_query_timeout(),
        )
        checkpoint()
    except ScanTimeout:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        checkpoint()
        return None, str(exc)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        return None, detail or f"git exited with {result.returncode}"
    return bool(result.stdout), None


def _measure_candidate(
    path: Path,
    now_ns: int,
    *,
    inspect_entry: Callable[[Path, os.stat_result], str | None] | None = None,
    tree: list[_TreeEntry] | None = None,
) -> tuple[int | None, Activity, str | None, tuple[_HardlinkEntry, ...]]:
    seen: set[tuple[int, int]] = set()
    hardlink_entries: list[_HardlinkEntry] = []
    total = 0
    newest = 0
    stack = [path]
    try:
        while stack:
            current = stack.pop()
            hook = _MEASURE_HOOK.get()
            if hook is not None:
                hook(current, current != path)
            checkpoint()
            info = current.stat(follow_symlinks=False)
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                return (
                    None,
                    Activity.UNCERTAIN,
                    f"protected: foreign owner uid {info.st_uid} at {current}",
                    (),
                )
            if inspect_entry is not None:
                reason = inspect_entry(current, info)
                if reason:
                    return None, Activity.UNCERTAIN, reason, ()
            if tree is not None:
                tree.append(
                    _TreeEntry(
                        current.relative_to(path).as_posix(),
                        _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_uid if hasattr(os, "geteuid") else None,
                    )
                )
            if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info):
                newest = max(newest, info.st_mtime_ns)
                continue
            if _is_mount(current):
                return (
                    None,
                    Activity.UNCERTAIN,
                    f"refusing to measure mount point at {current}",
                    (),
                )
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                return (
                    None,
                    Activity.UNCERTAIN,
                    f"unsupported filesystem entry: {current}",
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
            newest = max(newest, info.st_mtime_ns)
            if not stat.S_ISDIR(info.st_mode):
                continue
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    except ScanTimeout:
        raise
    except OSError as exc:
        return (
            None,
            Activity.UNCERTAIN,
            f"could not measure {current}: {exc}",
            (),
        )

    if tree is not None:
        tree.sort(key=lambda entry: entry.relative_path)
    activity = (
        Activity.OLD
        if newest < now_ns - MIN_AGE_SECONDS * 1_000_000_000
        else Activity.RECENT
    )
    return total, activity, None, tuple(hardlink_entries)
