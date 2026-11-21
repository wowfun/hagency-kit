"""Verified, relocatable FFmpeg release builds."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from packaging.version import Version

from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.tool_catalog import LANGUAGE_METHODS, Tool, probe
from hagency_cli.workspace.tool_process import external_metadata

GYAN = "https://api.github.com/repos/GyanD/codexffmpeg"
RIEDL = "https://ffmpeg.martin-riedl.de"


def supports(tool: Tool) -> bool:
    return tool.package == tool.command == "ffmpeg" and LANGUAGE_METHODS.isdisjoint(
        tool.methods
    )


def destination(root: Path) -> Path:
    return root / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")


def fetch(url: str):
    return urlopen(Request(url, headers={"User-Agent": "hagency-cli"}), timeout=60)


def read(url: str) -> str:
    with fetch(url) as response:
        return response.read().decode("utf-8")


def release(tool: Tool) -> tuple[str, str]:
    with external_metadata("portable FFmpeg release source"):
        return _release(tool)


def _release(tool: Tool) -> tuple[str, str]:
    machine = platform.machine().lower()
    if os.name == "nt" and machine in {"amd64", "x86_64"}:
        refs = json.loads(read(f"{GYAN}/git/matching-refs/tags/"))
        tags = [r["ref"].rsplit("/", 1)[-1] for r in refs]
        for tag in sorted(
            (t for t in tags if re.fullmatch(r"\d+(?:\.\d+)+", t)),
            key=Version,
            reverse=True,
        ):
            if not tool.accepts(Version(tag)):
                continue
            assets = json.loads(read(f"{GYAN}/releases/tags/{tag}"))["assets"]
            for asset in assets:
                if asset["name"] == f"ffmpeg-{tag}-essentials_build.zip":
                    digest = (asset.get("digest") or "").removeprefix("sha256:")
                    if not digest:
                        digest = read(
                            "https://www.gyan.dev/ffmpeg/builds/packages/"
                            + asset["name"]
                            + ".sha256"
                        ).split()[0]
                    return asset["browser_download_url"], digest
    elif sys.platform in {"darwin", "linux"} and machine in {
        "arm64",
        "aarch64",
        "amd64",
        "x86_64",
    }:
        arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
        platform_path = ("macos" if sys.platform == "darwin" else "linux") + "/" + arch
        history = read(f"{RIEDL}/info/history/{platform_path}/release")
        entries = re.findall(
            rf"/info/detail/{platform_path}/(\d+_(\d+(?:\.\d+)+))", history
        )
        for build, tag in sorted(entries, key=lambda e: Version(e[1]), reverse=True):
            if tool.accepts(Version(tag)):
                url = f"{RIEDL}/download/{platform_path}/{build}/ffmpeg.zip"
                return url, read(url + ".sha256").split()[0]
    fail(
        f"no portable FFmpeg build for {sys.platform}/{machine} matching {tool.version or '*'}"
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_destination(path: Path, previous_hash: str | None) -> None:
    if path.is_symlink() or (
        path.exists() and (not previous_hash or digest(path) != previous_hash)
    ):
        fail(f"unrecognized file at portable destination: {path}")


def change_portable(
    tool: Tool, path: Path, previous_hash: str | None, remove: bool
) -> str | None:
    verify_destination(path, previous_hash)
    if remove:
        path.unlink(missing_ok=True)
        return None
    url, expected = release(tool)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        archive = Path(tmp) / "build.zip"
        with fetch(url) as response, archive.open("wb") as target:
            shutil.copyfileobj(response, target)
        if digest(archive) != expected.lower():
            fail(f"FFmpeg download checksum mismatch for {url}")
        staged = Path(tmp) / path.name
        with zipfile.ZipFile(archive) as bundle:
            members = [
                item
                for item in bundle.infolist()
                if Path(item.filename).name == path.name
                and not item.is_dir()
                and not item.filename.startswith("__MACOSX/")
            ]
            if len(members) != 1:
                fail(f"expected one {path.name} in {url}")
            with bundle.open(members[0]) as source, staged.open("wb") as target:
                shutil.copyfileobj(source, target)
        staged.chmod(0o755)
        if probe(tool, staged).status != "available":
            fail(f"downloaded FFmpeg did not pass the version check: {url}")
        checksum = digest(staged)
        if path.exists():
            if not previous_hash or digest(path) != previous_hash:
                fail(f"portable destination changed: {path}")
            os.replace(staged, path)
        else:
            os.link(staged, path)
        return checksum
