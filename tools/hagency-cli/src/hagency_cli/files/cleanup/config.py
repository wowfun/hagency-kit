from __future__ import annotations

import hashlib
import os
import stat
import sys
import tomllib
from pathlib import Path
from types import MappingProxyType

from hagency_cli.files.cleanup.conditions import (
    entries,
    expand_rules,
    parse_condition,
    walk,
)
from hagency_cli.files.cleanup.models import (
    CleanupRule,
    ConfigSignature,
    RulesSnapshot,
    _unsafe_text,
    rule_command,
)
from hagency_cli.paths import expand_path
from hagency_cli.workspace.discovery import (
    _find_workspace_root,
    _installed_workspace_root,
)

CONFIG_NAME = "hagency-cleanup.toml"
DEFAULTS_PATH = Path(__file__).with_name("defaults.toml")
MAX_CONFIG_BYTES = 1024 * 1024


class CleanupConfigError(ValueError):
    pass


def _config_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read(path: Path, *, optional: bool = False) -> tuple[dict, ConfigSignature | None]:
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

        identity = _config_identity(after)
        if (
            _config_identity(before) != identity
            or _config_identity(path.stat()) != identity
        ):
            raise CleanupConfigError(f"cleanup config changed while reading: {path}")
        signature = ConfigSignature(identity, hashlib.sha256(data).hexdigest())
        return tomllib.loads(data.decode("utf-8")), signature
    except FileNotFoundError as exc:
        if optional and not path.is_symlink():
            return {}, None
        raise CleanupConfigError(f"missing cleanup config: {path}") from exc
    except CleanupConfigError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
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
        try:
            rule_command(identity)
        except ValueError as exc:
            raise CleanupConfigError(f"{exc}: {origin}") from exc
        if identity in seen:
            raise CleanupConfigError(
                f"duplicate cleanup rule id {identity!r}: {origin}"
            )
        seen.add(identity)
    return rows


def _rule(row: dict, origin: Path, previous: CleanupRule | None = None) -> CleanupRule:
    fields = {
        "id",
        "match",
        "exclude",
        "enabled",
        "auto_apply",
        "recursive",
        "include_unverified",
        "min_age_days",
        "preselect_after_days",
        "keep_previous",
    }
    unknown = set(row) - fields
    if unknown:
        raise CleanupConfigError(
            f"unsupported fields in cleanup rule {row['id']}: {sorted(unknown)}; use match conditions"
        )
    values = {key: getattr(previous, key) for key in fields} if previous else {}
    values.update(row)
    try:
        if "match" in row:
            values["match"] = parse_condition(
                row["match"], origin.parent, rule_command(row["id"])
            )
        elif previous is None:
            raise ValueError("missing match condition")
        patterns = values.get("exclude", ())
        if not isinstance(patterns, (list, tuple)) or any(
            not isinstance(p, str) or "\x00" in p for p in patterns
        ):
            raise ValueError("exclude must contain path patterns")
        from hagency_cli.files.cleanup.matching import compile_patterns

        values["exclude"] = tuple(patterns)
        compile_patterns(values["exclude"])
        for key, default in (
            ("enabled", True),
            ("auto_apply", False),
            ("recursive", False),
            ("include_unverified", False),
        ):
            if type(values.get(key, default)) is not bool:
                raise ValueError(f"{key} must be boolean")
        for key in ("min_age_days", "preselect_after_days", "keep_previous"):
            if key in values and (
                type(values[key]) is not int or not 0 <= values[key] <= 2**63 - 1
            ):
                raise ValueError(f"{key} must be a nonnegative 64-bit integer")
        if previous is None:
            sources = [
                node.value for node in walk(values["match"]) if node.op == "source"
            ]
            if not sources or any(
                source in {"project", "cachedir-tag"} for source in sources
            ):
                values.setdefault("preselect_after_days", 7)
        values["origin"] = str(origin)
        return CleanupRule(**values)
    except (TypeError, ValueError) as exc:
        raise CleanupConfigError(
            f"invalid cleanup rule {row['id']} in {origin}: {exc}"
        ) from exc


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
    try:
        expressions = expand_rules(tuple(rules.values()))
        for expression in expressions.values():
            entries(expression)  # Reject ambiguous lexical entries before any scan.
        for rule in rules.values():
            if rule.keep_previous != 1 and not any(
                node.op == "source" and node.value.endswith(".versions")
                for node in walk(expressions[rule.id])
            ):
                raise ValueError(f"keep_previous requires a versions source: {rule.id}")
    except ValueError as exc:
        raise CleanupConfigError(str(exc)) from exc
    return RulesSnapshot(
        tuple(rules.values()), tuple(files), MappingProxyType(expressions)
    )


def configuration_unchanged(snapshot: RulesSnapshot) -> bool:
    try:
        for path, signature in snapshot.files:
            if signature is None:
                # A newly created file or dangling symlink changes discovery.
                if path.exists() or path.is_symlink():
                    return False
                continue
            info = path.stat()
            if _config_identity(info) != signature.identity:
                return False
            # POSIX ctime detects in-place rewrites even with restored mtime.
            # Windows ctime can be creation time; retain content checks there,
            # and when the filesystem cannot supply a stable inode identity.
            if sys.platform == "win32" or not info.st_ino:
                if _read(path)[1] != signature:
                    return False
        return True
    except (CleanupConfigError, OSError):
        return False
