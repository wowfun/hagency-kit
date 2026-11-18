from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from hagency_cli.paths import expand_path, normalize_windows_shell_path


def create_windows_junction(link: Path, target: Path) -> None:
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "New-Item -ItemType Junction -ErrorAction Stop "
        "-Path $env:HAGENCY_JUNCTION_LINK "
        "-Target ([WildcardPattern]::Escape($env:HAGENCY_JUNCTION_TARGET)) | Out-Null"
    )
    # New-Item can convert an existing empty directory into a junction.
    # On Windows, rename fails atomically if the destination is occupied.
    with TemporaryDirectory(prefix=".hgc-junction-", dir=link.parent) as directory:
        staging_link = Path(directory) / "link"
        env = {
            **os.environ,
            "HAGENCY_JUNCTION_LINK": str(staging_link),
            "HAGENCY_JUNCTION_TARGET": str(target),
        }
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        staging_link.rename(link)


def create_symbolic_link(target: str, destination: str, *, cwd: Path) -> None:
    target = os.path.expanduser(normalize_windows_shell_path(target))
    link = expand_path(destination, cwd)
    if link.is_dir():
        name = Path(target).name
        if name in {"", ".."}:
            raise ValueError(f"target has no name to use inside {link}: {target}")
        link /= name
    elif not os.path.basename(destination):
        raise NotADirectoryError(f"destination is not an existing directory: {link}")

    if not link.parent.is_dir():
        raise NotADirectoryError(
            f"link parent is not an existing directory: {link.parent}"
        )
    if os.path.lexists(link):
        raise FileExistsError(f"link path already exists: {link}")

    if os.name == "nt":
        target_path = Path(os.path.abspath(link.parent.resolve(strict=True) / target))
        try:
            target_info = target_path.stat()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise FileNotFoundError(f"target does not exist: {target_path}") from exc
        if stat.S_ISDIR(target_info.st_mode):
            create_windows_junction(link, target_path)
            return

    os.symlink(target, link)
