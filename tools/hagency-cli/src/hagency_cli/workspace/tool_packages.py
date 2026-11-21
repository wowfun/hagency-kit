"""Global npm, pnpm and uv installations and their package-owned entry points."""

from __future__ import annotations

import filecmp
import json
import os
import shlex
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress
from hagency_cli.workspace.tool_catalog import Tool, version_range
from hagency_cli.workspace.tool_process import (
    external_metadata,
    node_script,
    process,
    query_json,
)


def environment(method: str, root: Path | None) -> dict:
    env = os.environ.copy()
    if root is None:
        return env
    if method == "uv":
        env.update(UV_TOOL_DIR=str(root / "uv"), UV_TOOL_BIN_DIR=str(root / "bin"))
    if method == "npm":
        env["npm_config_prefix"] = str(root / "npm")
    if method == "pnpm":
        home = root / "pnpm"
        env["PNPM_HOME"] = str(home)
        env["PATH"] = os.pathsep.join(
            (str(home / "bin"), str(home), env.get("PATH", ""))
        )
    return env


def uv_receipt(tool: Tool, env: dict) -> tuple[Path, dict]:
    directory = Path(env.get("UV_TOOL_DIR", ""))
    if not directory.is_absolute():
        directory = Path(process(["uv", "tool", "dir"], env=env).stdout.strip())
    directory /= canonicalize_name(Requirement(tool.package).name)
    receipt = directory / "uv-receipt.toml"
    if not receipt.is_file():
        return directory, {}
    with external_metadata(str(receipt)):
        record = tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]
        if not isinstance(record, dict):
            raise TypeError("expected a [tool] table")
        return directory, record


def uv_has_extras(tool: Tool, root: Path | None) -> bool:
    requirement = Requirement(tool.package)
    _, receipt = uv_receipt(tool, environment("uv", root))
    with external_metadata("uv receipt requirements"):
        return any(
            canonicalize_name(item["name"]) == canonicalize_name(requirement.name)
            and tool.extras.issubset(item.get("extras", ()))
            for item in receipt.get("requirements", ())
        )


def owned_entry(tool: Tool, method: str, root: Path | None) -> Path | None:
    if root and not (root / method).exists():
        return None
    with external_metadata(f"{method} package metadata for {tool.package}"):
        return _owned_entry(tool, method, root)


def _owned_entry(tool: Tool, method: str, root: Path | None) -> Path | None:
    env = environment(method, root)
    if method == "uv":
        directory, receipt = uv_receipt(tool, env)
        for entry in receipt.get("entrypoints", ()):
            if entry["name"].removesuffix(".exe") != tool.command:
                continue
            exe = Path(entry["install-path"])
            original = directory / ("Scripts" if os.name == "nt" else "bin") / exe.name
            if (
                exe.is_file()
                and original.is_file()
                and (
                    exe.samefile(original)
                    or (os.name == "nt" and filecmp.cmp(exe, original, shallow=False))
                )
            ):
                return exe
        return None

    if method == "npm":
        modules = Path(process(["npm", "root", "-g"], env=env).stdout.strip())
        directory = modules / tool.package
        if not directory.is_dir():
            return None
        bin_dir = modules.parent if os.name == "nt" else modules.parent.parent / "bin"
    else:
        rows = query_json(["pnpm", "list", "-g", "--depth=0", "--json"], env)
        if not isinstance(rows, list):
            raise ValueError("pnpm list must return an array")
        row = next(
            (row for row in rows if tool.package in row.get("dependencies", {})), None
        )
        if row is None:
            return None
        info = row["dependencies"][tool.package]
        directory = Path(
            info.get("path") or Path(row["path"]) / "node_modules" / tool.package
        )
        bin_dir = Path(process(["pnpm", "bin", "-g"], env=env).stdout.strip())
    metadata = directory / "package.json"
    if not metadata.is_file():
        return None
    pkg = json.loads(metadata.read_text(encoding="utf-8"))
    bins = pkg.get("bin", {})
    if isinstance(bins, str):
        bins = {tool.package.split("/")[-1]: bins}
    if not isinstance(bins, dict):
        raise TypeError("expected a package bin map")
    if pkg.get("name") != tool.package or tool.command not in bins:
        return None
    target = (directory / bins[tool.command]).resolve()
    shim = bin_dir / (tool.command + (".cmd" if os.name == "nt" else ""))
    if not shim.is_file() or not target.is_file():
        return None
    if shim.samefile(target):
        return shim
    if os.name == "nt":
        return shim if node_script(shim) == target else None
    # pnpm writes shell shims with the entry point relative to the bin directory.
    for line in shim.read_text(errors="replace").splitlines():
        if not line.lstrip().startswith("exec "):
            continue
        for word in shlex.split(line):
            directory_var, sep, relative = word.partition("/")
            if (
                sep
                and directory_var in {"$basedir", "$basedir_abs"}
                and (bin_dir / relative).resolve() == target
            ):
                return shim
    return None


def npm_spec(tool: Tool, method: str, env: dict) -> str:
    if not version_range(tool.version):
        return tool.package + "@latest"
    versions = query_json([method, "view", tool.package, "versions", "--json"], env)
    if not isinstance(versions, (str, list)) or (
        isinstance(versions, list) and not all(isinstance(v, str) for v in versions)
    ):
        fail(f"{method} returned invalid version metadata for {tool.package}")
    candidates = []
    for raw in versions if isinstance(versions, list) else [versions]:
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if not version.is_prerelease and tool.accepts(version):
            candidates.append((version, raw))
    if not candidates:
        fail(f"{method}: no stable version of {tool.package} matches {tool.version}")
    return f"{tool.package}@{max(candidates)[1]}"


def change_package(
    tool: Tool,
    method: str,
    root: Path | None,
    action: str,
    args: tuple[str, ...],
    current: Version | None,
    progress: Progress | None = None,
) -> None:
    env = environment(method, root)
    if action == "uninstall":
        verbs = {
            "npm": ["uninstall", "-g"],
            "pnpm": ["remove", "-g"],
            "uv": ["tool", "uninstall"],
        }
        argv = [method, *verbs[method], tool.package.partition("[")[0]]
    elif method == "uv":
        # An ensure operation can add extras while preserving a compatible version.
        spec = (
            f"=={current}"
            if action == "install" and current and tool.accepts(current)
            else str(version_range(tool.version))
        )
        argv = ["uv", "tool", "install", "--upgrade", tool.package + spec, *args]
    else:
        spec = npm_spec(tool, method, env)
        argv = [method, "install" if method == "npm" else "add", "-g", spec, *args]
    process(argv, env=env, progress=progress)
