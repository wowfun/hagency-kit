from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("Error: Python 3.11+ is required for stdlib tomllib.\n")
    raise SystemExit(1)


def die(message: str) -> None:
    sys.stderr.write(f"Error: {message}\n")
    raise SystemExit(1)


def expand_path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = base / path
    return path


def read_toml(path: Path) -> dict:
    if not path.exists():
        die(f"missing config: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def run(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> subprocess.CompletedProcess[str] | None:
    prefix = f"(cd {cwd} && " if cwd else ""
    suffix = ")" if cwd else ""
    print("+ " + prefix + " ".join(cmd) + suffix)
    if dry_run:
        return None
    return subprocess.run(cmd, cwd=cwd, check=True, text=True)


def git_ok(cmd: list[str], *, cwd: Path) -> bool:
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    ref: str


@dataclass(frozen=True)
class Source:
    name: str
    path: Path
    skills_path: str
    remote: Remote | None

    @property
    def skills_root(self) -> Path:
        path = Path(self.skills_path)
        if path.is_absolute():
            return path
        return self.path / path


def resolve_sources(registry: dict, *, repo_root: Path, checkout_override: str | None) -> dict[str, Source]:
    defaults = registry.get("defaults", {})
    checkout_dir_value = checkout_override or defaults.get("checkout_dir")
    checkout_dir = expand_path(checkout_dir_value, repo_root) if checkout_dir_value else None
    default_skills_path = defaults.get("skills_path", ".")
    default_remote_name = defaults.get("remote_name", "origin")
    default_remote_ref = defaults.get("remote_ref", "main")

    sources: dict[str, Source] = {}
    for raw_source in registry.get("sources", []):
        name = raw_source.get("name")
        if not name:
            die("source is missing required field: name")
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
            skills_path=raw_source.get("skills_path") or default_skills_path,
            remote=remote,
        )

    return sources


def profile_config_path(repo_root: Path, profile_name: str) -> Path:
    return repo_root / "profiles" / profile_name / "config.toml"


def profile_source_names(profile: dict) -> list[str]:
    names = []
    for entry in profile.get("skills", []):
        source_name = entry.get("source")
        if not source_name:
            die("profile skill entry is missing required field: source")
        names.append(source_name)
    return names


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


def sync_source(source: Source, *, dry_run: bool) -> None:
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
        run(["git", "fetch", remote.name], cwd=source.path, dry_run=dry_run)
        if not dry_run:
            remote_branch = f"refs/remotes/{remote.name}/{remote.ref}"
            local_branch = f"refs/heads/{remote.ref}"
            if git_ok(["git", "rev-parse", "--verify", local_branch], cwd=source.path):
                run(["git", "checkout", remote.ref], cwd=source.path, dry_run=False)
                run(["git", "pull", "--ff-only", remote.name, remote.ref], cwd=source.path, dry_run=False)
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
    run(["git", "clone", "--origin", remote.name, "--branch", remote.ref, remote.url, str(source.path)], dry_run=dry_run)


def require_source_path(source: Source) -> None:
    if not source.path.exists():
        if source.remote is None:
            die(f"local source path does not exist: {source.path}")
        die(f"source path does not exist; run hagency skill --sync-external first: {source.path}")
    if not source.path.is_dir():
        die(f"source path is not a directory: {source.path}")


def iter_links(entry: dict, source: Source) -> list[tuple[str, Path]]:
    mode = entry.get("mode", "source")
    root = source.skills_root

    if mode == "source":
        return [(entry.get("name") or source.name, root)]

    if mode == "expand":
        if not root.exists():
            die(f"skills root does not exist: {root}")
        links = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_dir() and (child / "SKILL.md").exists():
                links.append((child.name, child))
        return links

    if mode == "skill":
        child_path = entry.get("path")
        if not child_path:
            die(f"skill mode for source {source.name} requires path")
        target = root / child_path
        return [(entry.get("name") or Path(child_path).name, target)]

    die(f"unsupported mode for source {source.name}: {mode}")


def link_one(link_dir: Path, name: str, target: Path, *, dry_run: bool) -> None:
    if not target.exists() and not dry_run:
        die(f"link target does not exist: {target}")
    real_target = target.resolve() if target.exists() else target.absolute()
    link = link_dir / name
    if link.is_symlink():
        existing = link.resolve()
        if existing == real_target:
            print(f"ok {link} -> {real_target}")
            return
        print(f"+ rm {link}")
        if not dry_run:
            link.unlink()
    elif link.exists():
        die(f"refusing to overwrite non-symlink: {link}")

    print(f"+ ln -s {real_target} {link}")
    if not dry_run:
        link_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(real_target, link, target_is_directory=real_target.is_dir())


def init_profile(profile: dict, sources: dict[str, Source], target_dir: Path, *, dry_run: bool) -> None:
    link_dir = target_dir / ".agents" / "skills"
    for entry in profile.get("skills", []):
        source_name = entry.get("source")
        if not source_name:
            die("profile skill entry is missing required field: source")
        source = sources.get(source_name)
        if source is None:
            die(f"profile references unknown source: {source_name}")
        require_source_path(source)
        for link_name, target in iter_links(entry, source):
            link_one(link_dir, link_name, target, dry_run=dry_run)
