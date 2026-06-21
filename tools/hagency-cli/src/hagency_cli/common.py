from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("Error: Python 3.11+ is required for stdlib tomllib.\n")
    raise SystemExit(1)


def die(message: str) -> None:
    sys.stderr.write(f"Error: {message}\n")
    raise SystemExit(1)


def normalize_windows_shell_path(value: str) -> str:
    if os.name != "nt":
        return value
    match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", value)
    if not match:
        return value
    drive = match.group(1).upper()
    rest = match.group(2)
    if not rest:
        return f"{drive}:/"
    return f"{drive}:/{rest}"


def expand_path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(normalize_windows_shell_path(value)))
    if not path.is_absolute():
        path = base / path
    return path


def read_toml(path: Path) -> dict:
    if not path.exists():
        die(f"missing config: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def write_toml(path: Path, data: dict) -> None:
    content = render_toml(data)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    die(f"unsupported TOML value type: {type(value).__name__}")


def toml_key_part(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return toml_value(value)


def toml_table(parts: list[str]) -> str:
    return "[" + ".".join(toml_key_part(part) for part in parts) + "]"


def append_scalar_lines(lines: list[str], mapping: dict, keys: list[str]) -> None:
    seen = set()
    for key in keys:
        if key in mapping and mapping[key] is not None:
            lines.append(f"{key} = {toml_value(mapping[key])}")
            seen.add(key)
    for key, value in mapping.items():
        if key in seen or isinstance(value, dict | list) or value is None:
            continue
        lines.append(f"{key} = {toml_value(value)}")


def render_toml(data: dict) -> str:
    lines: list[str] = []
    top_level = {
        key: value
        for key, value in data.items()
        if key not in {"defaults", "source", "skill"} and not isinstance(value, dict | list) and value is not None
    }
    if top_level:
        append_scalar_lines(lines, top_level, ["name", "description"])

    defaults = data.get("defaults")
    if defaults:
        if lines:
            lines.append("")
        lines.append("[defaults]")
        append_scalar_lines(lines, defaults, ["checkout_dir", "depth", "remote_name", "remote_ref"])

    for name, raw_source in data.get("source", {}).items():
        if lines:
            lines.append("")
        lines.append(toml_table(["source", name]))
        append_scalar_lines(lines, raw_source, ["path"])
        remote = raw_source.get("remote")
        if remote:
            lines.append("")
            lines.append(toml_table(["source", name, "remote"]))
            append_scalar_lines(lines, remote, ["url", "name", "ref"])

    for name, raw_skill in data.get("skill", {}).items():
        if lines:
            lines.append("")
        lines.append(toml_table(["skill", name]))
        append_scalar_lines(lines, raw_skill, ["include", "exclude"])

    return "\n".join(lines).rstrip() + "\n"


def run(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> subprocess.CompletedProcess[str] | None:
    if cwd:
        print(f"+ cwd: {cwd}")
    print("+ cmd: " + " ".join(shlex.quote(part) for part in cmd))
    if dry_run:
        return None
    return subprocess.run(cmd, cwd=cwd, check=True, text=True)


def git_ok(cmd: list[str], *, cwd: Path) -> bool:
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
