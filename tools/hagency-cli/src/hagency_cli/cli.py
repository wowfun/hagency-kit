from __future__ import annotations

import argparse
import os
from pathlib import Path

from .profiles import (
    die,
    expand_path,
    init_profile,
    profile_config_path,
    profile_source_names,
    read_toml,
    resolve_sources,
    select_sources,
    sync_source,
)


def repo_root_arg(value: str | None) -> Path:
    raw = value or os.environ.get("HAGENCY_KIT_ROOT") or "."
    return expand_path(raw, Path.cwd()).resolve()


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Hagency Kit repository root (default: current directory)")
    parser.add_argument("--registry", default="skills/config.toml", help="Registry config path (default: skills/config.toml)")
    parser.add_argument("--checkout-dir", help="Override defaults.checkout_dir")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without changing files")


def load_sources(args: argparse.Namespace, repo_root: Path) -> dict:
    registry = read_toml(expand_path(args.registry, repo_root))
    return resolve_sources(registry, repo_root=repo_root, checkout_override=args.checkout_dir)


def sync_external_skills(args: argparse.Namespace) -> None:
    if not args.sync_external:
        die("skill command requires --sync-external/-se")

    repo_root = repo_root_arg(args.repo)
    sources = load_sources(args, repo_root)

    selected_names = list(args.sources)
    if args.profile:
        profile = read_toml(profile_config_path(repo_root, args.profile))
        selected_names.extend(profile_source_names(profile))

    selected = select_sources(sources, selected_names) if selected_names else list(sources.values())
    for source in selected:
        sync_source(source, dry_run=args.dry_run)


def init_profile_command(args: argparse.Namespace) -> None:
    repo_root = repo_root_arg(args.repo)
    sources = load_sources(args, repo_root)
    profile = read_toml(profile_config_path(repo_root, args.name))
    target = expand_path(args.path, Path.cwd())
    init_profile(profile, sources, target, dry_run=args.dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hagency", description="Manage Hagency Kit profiles and external skill sources.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    skill_parser = subcommands.add_parser("skill", help="Manage skill sources.")
    skill_parser.add_argument("--sync-external", "-se", action="store_true", help="Sync external skill sources.")
    skill_parser.add_argument("sources", nargs="*", help="Optional source names to sync")
    skill_parser.add_argument("--profile", help="Sync only sources referenced by profiles/<name>/config.toml")
    add_common_options(skill_parser)
    skill_parser.set_defaults(func=sync_external_skills)

    profile_parser = subcommands.add_parser("profile", help="Manage profiles.")
    profile_subcommands = profile_parser.add_subparsers(dest="profile_command", required=True)

    init_parser = profile_subcommands.add_parser("init", help="Initialize a profile into a target directory.")
    init_parser.add_argument("--path", "-p", required=True, help="Target workspace directory")
    init_parser.add_argument("name", help="Profile name under profiles/")
    add_common_options(init_parser)
    init_parser.set_defaults(func=init_profile_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
