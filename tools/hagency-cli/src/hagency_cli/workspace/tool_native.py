"""Exact native package selection and ownership checks."""

from __future__ import annotations

import os
import re
import shutil
import sys
from itertools import islice
from pathlib import Path

from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress
from hagency_cli.workspace.tool_catalog import Tool, upstream_version
from hagency_cli.workspace.tool_process import external_metadata, process, query_json


def package_id(tool: Tool, method: str) -> str:
    if method == "winget" and tool.package == "ffmpeg":
        return "Gyan.FFmpeg"
    return tool.package


def winget_location(package: str) -> Path | None:
    if os.name != "nt":
        return None
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    direct = package + "_Microsoft.Winget.Source_8wekyb3d8bbwe"

    def location(parent, name):
        try:
            with winreg.OpenKey(parent, name) as key:
                if winreg.QueryValueEx(key, "WinGetPackageIdentifier")[0] == package:
                    value = winreg.QueryValueEx(key, "InstallLocation")[0]
                    if isinstance(value, str) and value and Path(value).is_dir():
                        return Path(value)
        except OSError:
            pass
        return None

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, path) as parent:
                if found := location(parent, direct):
                    return found
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    if found := location(parent, winreg.EnumKey(parent, index)):
                        return found
        except OSError:
            continue
    return None


def owned_entry(tool: Tool, method: str) -> Path | None:
    package = package_id(tool, method)
    if method == "winget":
        directory = winget_location(package)
        if directory:
            directory = directory.resolve()
            shared = {Path.home(), Path(directory.anchor)}
            shared.update(
                Path(value)
                for key in (
                    "ProgramFiles",
                    "ProgramFiles(x86)",
                    "SystemRoot",
                    "LocalAppData",
                    "ProgramData",
                )
                if (value := os.environ.get(key))
            )
            if directory in shared:
                fail(
                    f"WinGet InstallLocation for {package} is a shared directory: {directory}"
                )
            found = list(
                islice(
                    (p for p in directory.rglob(tool.command + ".exe") if p.is_file()),
                    2,
                )
            )
            if len(found) == 1 and found[0].resolve().is_relative_to(
                directory.resolve()
            ):
                return found[0]
            fail(
                f"WinGet cannot identify a unique {tool.command}.exe for {package} in {directory}"
            )
        return None
    if method == "brew":
        result = process(["brew", "--prefix", "--installed", package], check=False)
        if result.returncode == 0:
            path = Path(result.stdout.strip()) / "bin" / tool.command
            if path.is_file():
                return path
        return None
    argv = {
        "apt": ["dpkg-query", "-L"],
        "dnf": ["rpm", "-ql"],
        "pacman": ["pacman", "-Qlq"],
    }[method]
    result = process([*argv, package], check=False)
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        path = Path(line)
        if (
            path.is_absolute()
            and path.parent.name in {"bin", "sbin"}
            and path.name == tool.command
            and path.is_file()
        ):
            return path
    return None


def candidates(method: str, package: str) -> list[str]:
    if method == "winget":
        result = process(
            [
                "winget",
                "show",
                "--source",
                "winget",
                "--id",
                package,
                "--exact",
                "--versions",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            check=False,
        )
        return (
            re.findall(r"(?m)^\s*(\d[\w.+-]*)\s*$", result.stdout)
            if not result.returncode
            else []
        )
    if method == "brew":
        metadata = query_json(["brew", "info", "--formula", "--json=v2", package])
        with external_metadata(f"brew info for {package}"):
            if "formulae" not in metadata and isinstance(metadata.get("casks"), list):
                return []
            formulae = metadata["formulae"]
            if not isinstance(formulae, list):
                raise TypeError("expected an array of formulae")
            versions = [
                f["versions"]["stable"]
                for f in formulae
                if package in (f["name"], f.get("full_name"))
            ]
            if not all(isinstance(v, str) for v in versions):
                raise TypeError("expected stable version strings")
            return versions
    if method == "apt":
        lines = process(["apt-cache", "madison", package]).stdout.splitlines()
        return [
            line.split("|")[1].strip()
            for line in lines
            if "|" in line and line.split("|")[0].strip() == package
        ]
    if method == "dnf":
        return process(
            [
                "dnf",
                "-q",
                "repoquery",
                "--available",
                "--queryformat",
                "%{version}-%{release}",
                package,
            ]
        ).stdout.splitlines()
    output = process(
        ["pacman", "-Si", package], env={**os.environ, "LC_ALL": "C"}, check=False
    )
    return re.findall(r"(?m)^Version\s*:\s*(\S+)", output.stdout)


def compatible(tool: Tool, method: str) -> str | None:
    versions = []
    for raw in candidates(method, package_id(tool, method)):
        # Compare upstream versions, retaining the full distributor revision for installation.
        version = upstream_version(raw.split(":")[-1])
        if version is not None and not version.is_prerelease and tool.accepts(version):
            versions.append((version, raw))
    return max(versions, key=lambda pair: pair[0])[1] if versions else None


def change_native(
    tool: Tool,
    method: str,
    root: Path | None,
    action: str,
    version: str | None,
    args: tuple[str, ...],
    *,
    installed: bool = False,
    progress: Progress | None = None,
) -> None:
    package = package_id(tool, method)
    remove = action == "uninstall"
    if method == "winget":
        verb = action
        if not remove:
            verb = "upgrade" if installed else "install"
        argv = [
            "winget",
            verb,
            "--id",
            package,
            "--exact",
            "--source",
            "winget",
            "--disable-interactivity",
        ]
        if not remove:
            argv += [
                "--version",
                version,
                "--accept-source-agreements",
                "--accept-package-agreements",
            ]
            if root and verb == "install":
                argv += ["--scope", "user", "--location", str(root / tool.name)]
    elif method == "brew":
        verb = "uninstall" if remove else ("upgrade" if installed else "install")
        argv = ["brew", verb, "--formula", package]
    elif method == "apt":
        argv = [
            "apt-get",
            "remove" if remove else "install",
            "-y",
            package if remove else f"{package}={version}",
        ]
    elif method == "dnf":
        argv = [
            "dnf",
            "-y",
            "remove" if remove else "install",
            package if remove else f"{package}-{version}",
        ]
    else:
        argv = ["pacman", "-R" if remove else "-S", "--noconfirm", package]
    if method in {"apt", "dnf", "pacman"} and getattr(os, "geteuid", lambda: 0)() != 0:
        if not shutil.which("sudo"):
            fail(f"{method} needs root privileges")
        argv = ["sudo", *([] if sys.stdin.isatty() else ["-n"]), *argv]
    process([*argv, *args], progress=progress)
