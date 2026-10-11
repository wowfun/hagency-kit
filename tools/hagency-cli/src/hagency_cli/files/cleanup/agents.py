from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hagency_cli.files.cleanup.inspection import (
    _has_link_or_reparse_component,
    _is_within,
    _resolve_path,
    checkpoint,
)
from hagency_cli.files.cleanup.models import AGENTS as AGENTS
from hagency_cli.files.cleanup.models import _unsafe_text

SOURCE_TIMEOUT = 60.0


@dataclass(frozen=True)
class AgentEntry:
    path: Path
    root: Path
    version: tuple[int, int, int] | None = None
    reason: str = ""


def _home_path(variable: str, fallback: Path) -> Path:
    raw = os.environ.get(variable)
    if not raw:
        return fallback
    path = Path(raw)
    if not path.is_absolute() or _unsafe_text(raw):
        raise OSError(f"{variable} must be an absolute path")
    try:
        if _has_link_or_reparse_component(path):
            raise OSError(f"{variable} contains a linked path component")
    except FileNotFoundError as exc:
        if ".." in path.parts:
            raise OSError(
                f"cannot normalize {variable} through missing components"
            ) from exc
    return Path(os.path.abspath(path))


def codex_home() -> Path:
    return _home_path("CODEX_HOME", Path.home() / ".codex")


def source_roots(source: str) -> tuple[Path, ...]:
    home = Path.home()
    if source == "codex.versions":
        return (codex_home() / "packages/standalone/releases",)
    if source == "codex.staging":
        base = codex_home()
        return (
            base / "packages/standalone/releases",
            base / ".tmp/bundled-marketplaces",
            base / ".tmp/marketplaces/.staging",
        )
    if source == "claude.versions":
        return (home / ".local/share/claude/versions",)
    if source == "copilot.versions":
        cache = _home_path("XDG_CACHE_HOME", home / ".cache") / "copilot"
        roots = (
            _home_path("COPILOT_CACHE_HOME", cache) / "pkg/universal",
            cache / "pkg/universal",
            _home_path("COPILOT_HOME", home / ".copilot") / "pkg/universal",
            home / ".copilot/pkg/universal",
        )
        return tuple(dict.fromkeys(roots))
    raise ValueError(f"unknown agent source: {source}")


def managed_roots() -> tuple[Path, ...]:
    home = Path.home()
    desktop_roots = (
        _home_path("XDG_CONFIG_HOME", home / ".config"),
        _home_path("XDG_CACHE_HOME", home / ".cache"),
        _home_path("XDG_DATA_HOME", home / ".local/share"),
        home / ".config",
        home / ".cache",
        home / ".local/share",
    )
    roots = tuple(
        dict.fromkeys(
            (
                codex_home(),
                home / ".codex",
                home / ".claude",
                _home_path("CLAUDE_CONFIG_DIR", home / ".claude"),
                home / ".local/share/claude",
                home / ".local/state/claude",
                home / ".codex-app",
                *(
                    root / name
                    for root in desktop_roots
                    for name in ("Codex", "Claude", "claude", "codex")
                ),
                _home_path("COPILOT_HOME", home / ".copilot"),
                home / ".copilot",
                *(root.parent.parent for root in source_roots("copilot.versions")),
            )
        )
    )
    # Protect both the configured spelling and the real data directory when a
    # default installation/state path is a symlink.
    return tuple(
        dict.fromkeys(path for root in roots for path in (root, _resolve_path(root)))
    )


def protected_generic_path(
    path: Path, *, project: bool = False, roots: tuple[Path, ...] | None = None
) -> str | None:
    # Rules cannot relabel installed versions or durable agent state as a cache.
    for root in managed_roots() if roots is None else roots:
        if _is_within(path, root, allow_equal=True):
            if project and any(
                _is_within(path, _resolve_path(base) / "worktrees")
                for base in (codex_home(), Path.home() / ".claude")
            ):
                continue
            return "managed Agent data requires its registered cleanup source"
        if _is_within(root, path):
            return "directory contains protected Agent data"
    return None


def process_block_reason(agent: str, *, deadline: float | None = None) -> str | None:
    """Inspect same-user Linux processes without executing an agent or logging argv."""
    try:
        roots = source_roots(agent + ".versions")
        with os.scandir("/proc") as processes:
            for entry in processes:
                checkpoint(deadline)
                if not entry.name.isdecimal():
                    continue
                proc = Path(entry.path)
                try:
                    if proc.stat().st_uid != os.getuid():
                        continue
                    with (proc / "cmdline").open("rb") as stream:
                        cmdline = stream.read(128 * 1024 + 1)
                    if len(cmdline) > 128 * 1024:
                        return f"{agent} process state unknown: command line exceeds 128 KiB"
                    args = cmdline.split(b"\0")
                    args = [os.fsdecode(arg) for arg in args if arg]
                    exe = os.readlink(proc / "exe")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                executable = Path(exe.removesuffix(" (deleted)"))
                if any(
                    _is_within(executable, root, allow_equal=True) for root in roots
                ) or executable.name in {
                    agent,
                    f"{agent}-code-mode-host",
                }:
                    return f"{agent} is running"
                for arg in args:
                    checkpoint(deadline)
                    if not arg.startswith("/"):
                        continue
                    path = Path(arg)
                    if any(_is_within(path, root, allow_equal=True) for root in roots):
                        return f"{agent} is running"
                    if path.name in {
                        agent,
                        "npm-loader.js",
                        "index.js",
                        "app.js",
                        "cli.js",
                        "codex.js",
                    } and any(
                        part in {agent, "claude-code" if agent == "claude" else agent}
                        for part in path.parts
                    ):
                        return f"{agent} is running"
    except OSError as exc:
        return f"{agent} process state unknown: {exc}"
    return None


def _regular(path: Path, *, executable: bool = False) -> bool:
    try:
        info = path.lstat()
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_size > 0
            and (not executable or bool(info.st_mode & 0o111))
        )
    except FileNotFoundError:
        return False


def _metadata(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(f"unsafe installation metadata: {path}")
        data = stream.read(65537)
    if len(data) > 65536:
        raise OSError(f"installation metadata exceeds 64 KiB: {path}")
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise TypeError("expected an object")
        return result
    except (ValueError, TypeError) as exc:
        raise OSError(f"unknown installation metadata: {path}") from exc


def _codex_complete(path: Path, version: str, target: str) -> bool:
    # https://github.com/openai/codex/blob/main/scripts/install/install.sh
    manifest = path / "codex-package.json"
    if os.path.lexists(manifest):
        metadata = _metadata(manifest)
        if type(metadata.get("layoutVersion")) is not int:
            return False
        if any(
            metadata.get(key) != value
            for key, value in {
                "layoutVersion": 1,
                "version": version,
                "target": target,
                "variant": "codex",
                "entrypoint": "bin/codex",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            }.items()
        ):
            return False
        names = (
            "bin/codex",
            "bin/codex-code-mode-host",
            "codex-path/rg",
            "codex-resources/bwrap",
        )
    else:
        names = ("codex", "codex-resources/rg", "codex-resources/bwrap")
    return all(
        _regular(path / name, executable=True)
        and not _has_link_or_reparse_component(path / name)
        for name in names
    )


def _known_copilot_loader(path: Path) -> bool:
    if path.name not in {"npm-loader.js", "index.js"}:
        return False
    package = _metadata(path.parent / "package.json")
    return package.get("name") == "@github/copilot" and isinstance(
        package.get("version"), str
    )


def occupancy_block_reason(path: Path) -> str | None:
    """Open files and working directories also protect installer staging trees."""
    try:
        with os.scandir("/proc") as processes:
            for entry in processes:
                checkpoint()
                if not entry.name.isdecimal():
                    continue
                proc = Path(entry.path)
                try:
                    if proc.stat().st_uid != os.getuid():
                        continue
                    links = [proc / "cwd", proc / "exe"]
                    with os.scandir(proc / "fd") as descriptors:
                        links.extend(Path(fd.path) for fd in descriptors)
                    for link in links:
                        checkpoint()
                        try:
                            target = Path(os.readlink(link).removesuffix(" (deleted)"))
                        except (FileNotFoundError, ProcessLookupError):
                            continue
                        if _is_within(target, path, allow_equal=True):
                            return "Agent cleanup target is occupied by a process"
                except (FileNotFoundError, ProcessLookupError):
                    continue
    except OSError as exc:
        return f"Agent target occupancy is unknown: {exc}"
    return None


def _launcher_target(path: Path, entries: list[AgentEntry]) -> Path:
    if not path.is_symlink():
        raise OSError(f"cannot identify active version from launcher: {path}")
    target = _resolve_path(path, strict=True)
    for entry in entries:
        if _is_within(target, entry.path, allow_equal=True):
            return entry.path
    raise OSError(f"launcher points outside the recognized versions: {path}")


def version_inventory(
    source: str, keep_previous: int, *, deadline: float | None = None
) -> tuple[AgentEntry, ...]:
    entries: list[AgentEntry] = []
    agent = source.split(".")[0]
    for root in source_roots(source):
        checkpoint(deadline)
        if not root.exists() and not root.is_symlink():
            continue
        if _has_link_or_reparse_component(root):
            raise OSError(f"unsafe version root: {root}")
        with os.scandir(root) as children:
            for child in children:
                checkpoint(deadline)
                path = Path(child.path)
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    entries.append(
                        AgentEntry(path, root, reason="linked version is protected")
                    )
                    continue
                name = child.name
                if agent == "codex":
                    match = re.fullmatch(
                        r"(\d+)\.(\d+)\.(\d+)-(?:x86_64|aarch64)-unknown-linux-(?:musl|gnu)",
                        name,
                    )
                    complete = (
                        bool(match)
                        and stat.S_ISDIR(info.st_mode)
                        and _codex_complete(
                            path, ".".join(match.groups()), name.split("-", 1)[1]
                        )
                    )
                else:
                    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", name)
                    complete = (
                        _regular(path, executable=True)
                        if agent == "claude"
                        else stat.S_ISDIR(info.st_mode) and _regular(path / "app.js")
                    )
                version = (
                    tuple(map(int, match.groups())) if match and complete else None
                )
                entries.append(
                    AgentEntry(
                        path,
                        root,
                        version,
                        ""
                        if version is not None
                        else "prerelease, incomplete or unknown version is protected",
                    )
                )
    valid = [entry for entry in entries if entry.version is not None]
    if not valid:
        return tuple(entries)
    active: set[Path] = set()
    foreign_platforms: set[Path] = set()
    if agent == "codex":
        base = codex_home() / "packages/standalone"
        current = _launcher_target(base / "current", entries)
        active.add(current)
        target = current.name.split("-", 1)[1]
        foreign_platforms = {
            entry.path for entry in valid if entry.path.name.split("-", 1)[1] != target
        }
        valid = [entry for entry in valid if entry.path not in foreign_platforms]
        bin_dir = _home_path("CODEX_INSTALL_DIR", Path.home() / ".local/bin")
        for name in ("codex", "codex-code-mode-host"):
            launchers = {bin_dir / name}
            located = shutil.which(name)
            if located:
                launchers.add(Path(os.path.abspath(located)))
            for launcher in launchers:
                if launcher.exists() or launcher.is_symlink():
                    active.add(_launcher_target(launcher, entries))
        marker = base / "auto-update-version"
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file():
                raise OSError("unsafe Codex update version marker")
            with marker.open("r", encoding="utf-8") as stream:
                name = stream.read(512).strip()
            match = next((e.path for e in entries if e.path.name == name), None)
            if match is None:
                raise OSError("Codex update version marker is unknown")
            active.add(match)
    elif agent == "claude":
        launcher = Path.home() / ".local/bin/claude"
        active.add(_launcher_target(launcher, entries))
        located = shutil.which("claude")
        if located and _resolve_path(Path(located)) != _resolve_path(launcher):
            raise OSError("custom Claude launcher cannot be verified")
    else:
        # The published universal loader selects the highest complete stable
        # package across new and legacy cache roots. Never launch it to probe.
        highest = max(e.version for e in valid)
        active.update(e.path for e in valid if e.version == highest)
        located = shutil.which("copilot")
        if located:
            launcher = Path(located)
            resolved = _resolve_path(launcher, strict=True)
            direct = next(
                (
                    e.path
                    for e in entries
                    if _is_within(resolved, e.path, allow_equal=True)
                ),
                None,
            )
            if direct:
                active.add(direct)
            else:
                if not _known_copilot_loader(resolved):
                    raise OSError("custom Copilot launcher cannot be verified")
    active_versions = [e.version for e in valid if e.path in active]
    if not active_versions:
        raise OSError(f"{agent} active stable version cannot be verified")
    oldest_active = min(active_versions)
    fallback = sorted(
        {e.version for e in valid if e.version < oldest_active}, reverse=True
    )[:keep_previous]
    result = []
    for entry in entries:
        reason = entry.reason
        if entry.path in foreign_platforms:
            reason = "version for another platform is protected"
        elif entry.path in active:
            reason = "active or pending version is protected"
        elif entry.version is not None and entry.version >= oldest_active:
            reason = "pending newer version is protected"
        elif entry.version in fallback:
            reason = "rollback version is protected"
        result.append(AgentEntry(entry.path, entry.root, entry.version, reason))
    return tuple(result)


def staging_inventory(*, deadline: float | None = None) -> tuple[AgentEntry, ...]:
    entries = []
    for root, prefixes in zip(
        source_roots("codex.staging"),
        (
            (".staging.",),
            ("openai-bundled.staging-",),
            ("marketplace-add-", "marketplace-upgrade-"),
        ),
        strict=True,
    ):
        if not root.exists() and not root.is_symlink():
            continue
        if _has_link_or_reparse_component(root):
            raise OSError(f"unsafe staging root: {root}")
        with os.scandir(root) as children:
            for child in children:
                checkpoint(deadline)
                if child.name.startswith(prefixes) and child.is_dir(
                    follow_symlinks=False
                ):
                    entries.append(AgentEntry(Path(child.path), root))
    return tuple(entries)


@contextmanager
def installation_lock(candidate_path: Path, agent: str | None) -> Iterator[None]:
    if agent != "codex":
        yield
        return
    base = codex_home() / "packages/standalone"
    if not _is_within(candidate_path, base):
        yield
        return
    import fcntl

    if _has_link_or_reparse_component(base):
        raise OSError("unsafe Codex installation root")
    if os.path.lexists(base / "install.lock.d"):
        raise OSError("Codex installer directory lock is present")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(base / "install.lock", flags)
    except FileNotFoundError as exc:
        raise OSError(
            "Codex install.lock is missing; cannot synchronize with the installer"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("unsafe Codex install.lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = os.fstat(fd)
        current = (base / "install.lock").lstat()
        if (locked.st_dev, locked.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("Codex installation lock changed")
        if os.path.lexists(base / "install.lock.d"):
            raise OSError("Codex installer directory lock appeared")
        yield
    finally:
        os.close(fd)
