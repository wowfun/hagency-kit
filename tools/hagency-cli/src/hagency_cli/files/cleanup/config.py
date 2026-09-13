from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from pathlib import Path

from hagency_cli.files.cleanup.models import CleanupRule, RulesSnapshot, _unsafe_text
from hagency_cli.paths import expand_path
from hagency_cli.workspace.discovery import (
    _find_workspace_root,
    _installed_workspace_root,
)

CONFIG_NAME = "hagency-cleanup.toml"
DEFAULTS_PATH = Path(__file__).with_name("defaults.toml")
MAX_CONFIG_BYTES = 1024 * 1024
SOURCES = {
    "project",
    "cachedir-tag",
    "paths",
    "codex.versions",
    "claude.versions",
    "copilot.versions",
    "codex.staging",
}


class CleanupConfigError(ValueError):
    pass


def _read(path: Path, *, optional: bool = False) -> tuple[dict, str | None]:
    try:
        if _unsafe_text(str(path)):
            raise CleanupConfigError("unsafe characters in cleanup config path")
        if not stat.S_ISREG(path.stat().st_mode):
            raise CleanupConfigError(f"not a regular cleanup config: {path}")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise CleanupConfigError(f"not a regular cleanup config: {path}")
            data = stream.read(MAX_CONFIG_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_CONFIG_BYTES:
            raise CleanupConfigError(f"cleanup config exceeds 1 MiB: {path}")

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if identity(before) != identity(after) or identity(path.stat()) != identity(
            after
        ):
            raise CleanupConfigError(f"cleanup config changed while reading: {path}")
        signature = hashlib.sha256(
            data + repr((after.st_dev, after.st_ino, after.st_mtime_ns)).encode()
        ).hexdigest()
        return tomllib.loads(data.decode("utf-8")), signature
    except FileNotFoundError as exc:
        if optional and not path.is_symlink():
            return {}, None
        raise CleanupConfigError(f"missing cleanup config: {path}") from exc
    except CleanupConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise CleanupConfigError(f"cannot read cleanup config {path}: {exc}") from exc


def config_path(value: Path | None, cwd: Path) -> Path | None:
    if value is not None:
        return Path(os.path.abspath(expand_path(str(value), cwd)))
    # Selecting a workspace is separate from reading its source registry.
    root = _find_workspace_root(cwd) or _installed_workspace_root()
    return root / CONFIG_NAME if root is not None else None


def _rows(data: dict, origin: Path) -> list[dict]:
    if set(data) - {"schema_version", "rules"}:
        raise CleanupConfigError(f"unknown cleanup configuration fields in {origin}")
    version = data.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise CleanupConfigError(f"unsupported cleanup schema_version in {origin}")
    rows = data.get("rules", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CleanupConfigError(f"rules must be an array of tables: {origin}")
    seen = set()
    for row in rows:
        identity = row.get("id")
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or _unsafe_text(identity)
            or identity in seen
        ):
            raise CleanupConfigError(
                f"invalid or duplicate cleanup rule id {identity!r}: {origin}"
            )
        seen.add(identity)
    return rows


def _rule(row: dict, origin: Path, previous: CleanupRule | None = None) -> CleanupRule:
    fields = {
        "id",
        "command",
        "source",
        "include",
        "exclude",
        "enabled",
        "root",
        "min_age_days",
        "preselect_after_days",
        "keep_previous",
    }
    if set(row) - fields:
        raise CleanupConfigError(
            f"unknown fields in cleanup rule {row['id']}: {sorted(set(row) - fields)}"
        )
    values = {key: getattr(previous, key) for key in fields} if previous else {}
    if previous and any(
        key in row and row[key] != getattr(previous, key)
        for key in ("command", "source")
    ):
        raise CleanupConfigError(
            f"cannot change command/source of builtin rule {row['id']}"
        )
    values.update(row)
    source = values.get("source")
    command = values.get("command")
    if (
        not isinstance(source, str)
        or source not in SOURCES
        or command != ("purge" if source in {"project", "cachedir-tag"} else "clean")
    ):
        raise CleanupConfigError(f"invalid command/source in cleanup rule {row['id']}")
    if previous is None and source in {"project", "cachedir-tag", "paths"}:
        # Recent custom targets remain eligible for deliberate manual selection.
        values.setdefault("preselect_after_days", 7)
    for key in ("include", "exclude"):
        patterns = values.get(key, [])
        if not isinstance(patterns, (list, tuple)) or any(
            not isinstance(p, str) or "\x00" in p for p in patterns
        ):
            raise CleanupConfigError(f"{row['id']}.{key} must contain path patterns")
        values[key] = tuple(patterns)
        from hagency_cli.files.cleanup.matching import compile_patterns

        try:
            compile_patterns(values[key])
        except (ValueError, TypeError) as exc:
            raise CleanupConfigError(
                f"invalid pattern in {row['id']}.{key}: {exc}"
            ) from exc
    if not previous and "include" not in row:
        raise CleanupConfigError(
            f"missing include patterns in cleanup rule {row['id']}"
        )
    if type(values.get("enabled", True)) is not bool:
        raise CleanupConfigError(f"{row['id']}.enabled must be boolean")
    for key in ("min_age_days", "preselect_after_days", "keep_previous"):
        if key in values and (
            type(values[key]) is not int or not 0 <= values[key] <= 2**63 - 1
        ):
            raise CleanupConfigError(
                f"{row['id']}.{key} must be a nonnegative 64-bit integer"
            )
    if "keep_previous" in row and not source.endswith(".versions"):
        raise CleanupConfigError(f"keep_previous is not supported by {source}")
    if source != "paths" and "root" in row:
        raise CleanupConfigError(f"root is not supported by {source}")
    if source == "paths":
        root = values.get("root")
        if not isinstance(root, (str, Path)) or not str(root):
            raise CleanupConfigError(f"missing root in cleanup rule {row['id']}")
        if "root" in row:
            if not isinstance(root, str) or _unsafe_text(root):
                raise CleanupConfigError(f"invalid root in cleanup rule {row['id']}")
            values["root"] = Path(os.path.abspath(expand_path(root, origin.parent)))
    values["origin"] = str(origin)
    try:
        return CleanupRule(**values)
    except TypeError as exc:
        raise CleanupConfigError(f"invalid cleanup rule {row['id']}: {exc}") from exc


def load_rules(value: Path | None = None, *, cwd: Path | None = None) -> RulesSnapshot:
    defaults, signature = _read(DEFAULTS_PATH)
    rules = {
        row["id"]: _rule(row, DEFAULTS_PATH) for row in _rows(defaults, DEFAULTS_PATH)
    }
    files = [(DEFAULTS_PATH, signature)]
    selected = config_path(value, cwd or Path.cwd())
    if selected is not None:
        data, signature = _read(selected, optional=value is None)
        files.append((selected, signature))
        for row in _rows(data, selected):
            rules[row["id"]] = _rule(row, selected, rules.get(row["id"]))
    return RulesSnapshot(tuple(rules.values()), tuple(files))


def configuration_unchanged(snapshot: RulesSnapshot) -> bool:
    try:
        return all(
            _read(path, optional=signature is None)[1] == signature
            for path, signature in snapshot.files
        )
    except CleanupConfigError:
        return False
