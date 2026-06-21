from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from .common import die, expand_path, git_ok, read_toml, run

GIT_NETWORK_RETRIES = 3
GIT_NETWORK_RETRY_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    ref: str


@dataclass(frozen=True)
class Source:
    name: str
    path: Path
    remote: Remote | None


def resolve_sources(registry: dict, *, repo_root: Path, checkout_override: str | None) -> dict[str, Source]:
    if "sources" in registry:
        die("legacy [[sources]] config is no longer supported; use [source.<name>]")
    defaults = registry.get("defaults", {})
    checkout_dir_value = checkout_override or defaults.get("checkout_dir")
    checkout_dir = expand_path(checkout_dir_value, repo_root) if checkout_dir_value else None
    default_remote_name = defaults.get("remote_name", "origin")
    default_remote_ref = defaults.get("remote_ref", "main")

    sources: dict[str, Source] = {}
    for name, raw_source in registry.get("source", {}).items():
        if name in sources:
            die(f"duplicate source name: {name}")

        raw_remote = raw_source.get("remote")
        remote = None
        if raw_remote:
            url = raw_remote.get("url")
            if not url:
                die(f"source {name} remote is missing required field: url")
            remote = Remote(
                name=raw_remote.get("name") or default_remote_name,
                url=url,
                ref=raw_remote.get("ref") or default_remote_ref,
            )

        raw_path = raw_source.get("path")
        if raw_path:
            path = expand_path(raw_path, repo_root)
        elif remote and checkout_dir:
            path = checkout_dir / name
        else:
            die(f"source {name} needs path, or remote plus defaults.checkout_dir")

        sources[name] = Source(
            name=name,
            path=path,
            remote=remote,
        )

    return sources


def raw_source_by_name(registry: dict, name: str) -> dict | None:
    return registry.get("source", {}).get(name)


def is_git_url(value: str) -> bool:
    return (
        value.startswith(("http://", "https://", "ssh://"))
        or value.startswith("git@")
        or (":" in value and "/" in value and not value.startswith(("/", ".")))
    )


def source_url_path_parts(url: str) -> list[str]:
    raw = url.rstrip("/")
    if "://" in raw:
        path = urlparse(raw).path
    elif ":" in raw and "/" in raw:
        path = raw.rsplit(":", 1)[1]
    else:
        path = raw

    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return parts


def infer_source_name_from_url(url: str) -> str:
    parts = source_url_path_parts(url)
    if not parts or not parts[-1]:
        die(f"could not infer source name from URL: {url}")
    return parts[-1]


def infer_owner_source_name_from_url(url: str) -> str | None:
    parts = source_url_path_parts(url)
    if len(parts) < 2 or not parts[-2] or not parts[-1]:
        return None
    return "/".join(parts[-2:])


def resolve_source_add_args(source: str, *, name: str | None, url: str | None) -> tuple[str, str | None]:
    if is_git_url(source):
        if url:
            die("source add URL positional cannot be combined with --url")
        return (name or infer_source_name_from_url(source), source)
    if name:
        die("--name can only be used when the positional source is a URL")
    return (source, url)


def build_source_entry(
    *,
    name: str,
    url: str | None,
    path: str | None,
    ref: str | None,
    remote_name: str | None,
) -> tuple[str, dict]:
    if not url and not path:
        die("source add requires --url or --path")
    if not url and (ref or remote_name):
        die("--ref and --remote-name require --url")

    entry: dict[str, Any] = {}
    if path:
        entry["path"] = path
    if url:
        remote: dict[str, str] = {"url": url}
        if remote_name:
            remote["name"] = remote_name
        if ref:
            remote["ref"] = ref
        entry["remote"] = remote
    return (name, entry)


def add_source_entry(registry: dict, name: str, entry: dict) -> None:
    if raw_source_by_name(registry, name) is not None:
        die(f"source already exists: {name}")
    registry.setdefault("source", {})[name] = entry


def remove_source_entry(registry: dict, name: str) -> dict:
    sources = registry.get("source", {})
    if name in sources:
        return sources.pop(name)
    die(f"unknown source: {name}")


def find_profile_source_references(repo_root: Path, source_name: str) -> list[Path]:
    profiles_root = repo_root / "profiles"
    if not profiles_root.exists():
        return []

    references: list[Path] = []
    for profile_config in sorted(profiles_root.glob("*/config.toml")):
        profile = read_toml(profile_config)
        if "skills" in profile:
            die("legacy [[skills]] profile config is no longer supported; use [skill.<source>]")
        if source_name in profile.get("skill", {}):
            references.append(profile_config)
    return references


def select_sources(sources: dict[str, Source], names: list[str]) -> list[Source]:
    selected = []
    seen = set()
    for name in names:
        source = sources.get(name)
        if source is None:
            die(f"unknown source: {name}")
        if name not in seen:
            selected.append(source)
            seen.add(name)
    return selected


def depth_args(depth: int | None) -> list[str]:
    if depth is None:
        return []
    return ["--depth", str(depth)]


def git_network_label(cmd: list[str]) -> str:
    if cmd[:3] == ["git", "pull", "--ff-only"]:
        return "git pull --ff-only"
    if len(cmd) >= 2 and cmd[0] == "git":
        return f"git {cmd[1]}"
    return cmd[0]


def run_git_network(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> None:
    for attempt in range(GIT_NETWORK_RETRIES + 1):
        try:
            run(cmd, cwd=cwd, dry_run=dry_run)
            return
        except subprocess.CalledProcessError:
            if attempt == GIT_NETWORK_RETRIES:
                raise
            retry = attempt + 1
            print(f"retry {retry}/{GIT_NETWORK_RETRIES} after {git_network_label(cmd)} failed")
            time.sleep(GIT_NETWORK_RETRY_DELAY_SECONDS * (2 ** attempt))


def sync_source(source: Source, *, dry_run: bool, depth: int | None = None) -> None:
    remote = source.remote
    if source.path.exists():
        if remote is None:
            if not source.path.is_dir():
                die(f"local source is not a directory: {source.path}")
            return
        if not git_ok(["git", "rev-parse", "--is-inside-work-tree"], cwd=source.path):
            die(f"remote-bound source exists but is not a git repo: {source.path}")
        current_url = subprocess.run(
            ["git", "remote", "get-url", remote.name],
            cwd=source.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if current_url.returncode != 0:
            run(["git", "remote", "add", remote.name, remote.url], cwd=source.path, dry_run=dry_run)
        elif current_url.stdout.strip() != remote.url:
            run(["git", "remote", "set-url", remote.name, remote.url], cwd=source.path, dry_run=dry_run)
        run_git_network(["git", "fetch", *depth_args(depth), remote.name], cwd=source.path, dry_run=dry_run)
        if not dry_run:
            remote_branch = f"refs/remotes/{remote.name}/{remote.ref}"
            local_branch = f"refs/heads/{remote.ref}"
            if git_ok(["git", "rev-parse", "--verify", local_branch], cwd=source.path):
                run(["git", "checkout", remote.ref], cwd=source.path, dry_run=False)
                run_git_network(
                    ["git", "pull", "--ff-only", *depth_args(depth), remote.name, remote.ref],
                    cwd=source.path,
                    dry_run=False,
                )
            elif git_ok(["git", "rev-parse", "--verify", remote_branch], cwd=source.path):
                run(
                    ["git", "checkout", "-b", remote.ref, "--track", f"{remote.name}/{remote.ref}"],
                    cwd=source.path,
                    dry_run=False,
                )
            else:
                run(["git", "checkout", remote.ref], cwd=source.path, dry_run=False)
        else:
            run(["git", "checkout", remote.ref], cwd=source.path, dry_run=True)
        return

    if remote is None:
        die(f"local source path does not exist: {source.path}")
    run_git_network(
        ["git", "clone", "--origin", remote.name, "--branch", remote.ref, *depth_args(depth), remote.url, str(source.path)],
        dry_run=dry_run,
    )


def require_source_path(source: Source) -> None:
    if not source.path.exists():
        if source.remote is None:
            die(f"local source path does not exist: {source.path}")
        die(f"source path does not exist; run hagency source sync first: {source.path}")
    if not source.path.is_dir():
        die(f"source path is not a directory: {source.path}")
