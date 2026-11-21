"""Workspace and profile tool declarations and local command probes."""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from hagency_cli.workspace.config import read_toml, render_toml, write_toml
from hagency_cli.workspace.discovery import workspace_config_path
from hagency_cli.workspace.errors import WorkspaceError, fail
from hagency_cli.workspace.profiles import profile_config_path, read_profile_config
from hagency_cli.workspace.tool_process import process

LANGUAGE_METHODS = {"pnpm", "npm", "uv"}
METHODS = LANGUAGE_METHODS | {"winget", "brew", "apt", "dnf", "pacman"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
VERSION_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:-?(?:alpha|beta|rc|a|b)\.?\d+)?)",
    re.IGNORECASE,
)


def system_methods() -> tuple[str, ...]:
    if os.name == "nt":
        return ("winget",)
    if sys.platform == "darwin":
        return ("brew",)
    return ("apt", "dnf", "pacman")


def version_range(value: str) -> SpecifierSet:
    if not value or value == "*":
        return SpecifierSet()
    try:
        return SpecifierSet("==" + value if value[0].isdigit() else value)
    except InvalidSpecifier:
        fail(f"invalid tool version range: {value!r}")


def upstream_version(text: str) -> Version | None:
    # Debian/RPM package versions may start with an epoch (7:6.1.1).
    line = text.strip().splitlines()[:1]
    if line:
        for word in line[0].split():
            candidate = re.sub(r"^\d+:", "", word.strip("(),;"))
            try:
                return Version(candidate)
            except InvalidVersion:
                # FFmpeg and native packages can append distributor build labels.
                match = VERSION_TOKEN.match(candidate)
                if match:
                    return Version(match[1])
    return None


@dataclass(frozen=True)
class Tool:
    name: str
    command: str
    package: str
    version: str
    methods: tuple[str, ...]
    version_args: tuple[str, ...]
    install_args: tuple[str, ...]
    extras: frozenset[str]

    def accepts(self, version: Version) -> bool:
        return version_range(self.version).contains(version, prereleases=True)


def _table(doc: dict) -> dict:
    table = doc.get("tool", {})
    if not isinstance(table, dict):
        fail("tool must be a table of named tool entries")
    for name, entry in table.items():
        if not IDENTIFIER.fullmatch(name) or not isinstance(entry, dict):
            fail(f"invalid tool entry: {name!r}")
    return table


def effective_tools(registry: dict, profile: dict) -> dict[str, Tool]:
    base, overrides = _table(registry), _table(profile)
    result = {}
    for name in dict.fromkeys((*base, *overrides)):
        entry = {**base.get(name, {}), **overrides.get(name, {})}
        fields = {
            "enabled",
            "command",
            "package",
            "version",
            "install",
            "version_args",
            "install_args",
        }
        if unknown := entry.keys() - fields:
            fail(f"tool {name}: unknown fields {', '.join(sorted(unknown))}")
        if type(entry.get("enabled", True)) is not bool:
            fail(f"tool {name}: enabled must be a boolean")
        if not entry.get("enabled", True):
            continue
        for key in ("command", "package", "version"):
            if key in entry and not isinstance(entry[key], str):
                fail(f"tool {name}: {key} must be a string")
        command = entry.get("command", name)
        if not IDENTIFIER.fullmatch(command):
            fail(f"tool {name}: invalid command {command!r}")
        package = entry.get("package", name)
        if not re.fullmatch(
            r"(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9.+_-]*(?:\[[A-Za-z0-9._,-]+\])?",
            package,
        ):
            fail(f"tool {name}: invalid package name or extras {package!r}")
        for key in ("install", "version_args", "install_args"):
            if key not in entry:
                continue
            items = entry[key]
            if key == "install" and isinstance(items, str):
                items = entry[key] = [items]
            if not isinstance(items, list) or not all(
                isinstance(i, str) and "\0" not in i for i in items
            ):
                fail(f"tool {name}: {key} must be an array of strings")
        methods = tuple(entry.get("install", system_methods()))
        if not methods or set(methods) - METHODS:
            fail(f"tool {name}: invalid installation methods {methods}")
        extras = frozenset()
        if "uv" in methods:
            try:
                extras = frozenset(Requirement(package).extras)
            except InvalidRequirement:
                fail(f"tool {name}: invalid uv package or extras {package!r}")
        version = entry.get("version", "")
        version_range(version)
        result[name] = Tool(
            name,
            command,
            package,
            version,
            methods,
            tuple(
                entry.get(
                    "version_args", ["-version" if command == "ffmpeg" else "--version"]
                )
            ),
            tuple(entry.get("install_args", [])),
            extras,
        )
    return result


def catalog(root: Path, profile: str | None) -> dict[str, Tool]:
    return effective_tools(
        read_toml(workspace_config_path(root)),
        read_profile_config(root, profile) if profile else {},
    )


def select_tools(tools: dict[str, Tool], name: str | None) -> list[Tool]:
    if name is None:
        return list(tools.values())
    if name not in tools:
        fail(f"tool is not in the effective catalog: {name}")
    return [tools[name]]


def edit_tool(
    root: Path, profile: str | None, name: str, updates: dict | None, dry_run: bool
) -> str:
    path = (
        profile_config_path(root, profile) if profile else workspace_config_path(root)
    )
    registry = read_toml(workspace_config_path(root))
    doc = read_profile_config(root, profile) if profile else registry
    table = doc.setdefault("tool", {})
    _table(doc)
    if updates is None:
        select_tools(effective_tools(registry, doc if profile else {}), name)
        if profile and name in _table(registry):
            table[name] = {"enabled": False}
        else:
            del table[name]
    else:
        entry = table.setdefault(name, {})
        if profile and not _table(registry).get(name, {}).get("enabled", True):
            entry["enabled"] = True
        elif entry.get("enabled") is False:
            entry.pop("enabled")
        entry.update(
            {key: value for key, value in updates.items() if value is not None}
        )
    effective_tools(registry, doc if profile else {})
    if not dry_run:
        write_toml(path, doc)
    return render_toml(doc) if dry_run else str(path)


@dataclass(frozen=True)
class Status:
    path: Path | None
    version: Version | None
    status: str

    def display(self, name: str) -> str:
        return f"{name}\t{self.status}\t{self.version or '-'}\t{self.path or '-'}"


def probe(tool: Tool, path: Path | None = None) -> Status:
    if path is None and (found := shutil.which(tool.command)):
        path = Path(found).absolute()
    if path is None or not path.is_file():
        return Status(None, None, "missing")
    try:
        # Python CLIs can import large converter libraries on their first run.
        output = process([str(path), *tool.version_args], timeout=60, check=False)
    except WorkspaceError:
        return Status(path, None, "unavailable")
    if output.returncode:
        # Some working commands have no version switch. Other failures (e.g. a
        # missing runtime dependency) also prevent the tool from being used.
        diagnostic = (output.stderr or output.stdout).lower()
        unsupported = any(
            s in diagnostic
            for s in (
                "unknown option",
                "unrecognized argument",
                "unrecognized option",
                "unknown flag",
            )
        )
        if not unsupported:
            return Status(path, None, "unavailable")
        version = None
    else:
        version = upstream_version(output.stdout or output.stderr)
    if version is not None:
        state = "available" if tool.accepts(version) else "version-mismatch"
    else:
        state = "version-unknown" if version_range(tool.version) else "available"
    return Status(path, version, state)
