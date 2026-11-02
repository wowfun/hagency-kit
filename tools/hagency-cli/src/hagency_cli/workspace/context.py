from __future__ import annotations

from pathlib import Path

from hagency_cli.workspace.config import read_toml
from hagency_cli.workspace.discovery import (
    resolve_workspace_root,
    workspace_config_path,
)
from hagency_cli.workspace.sources import resolve_sources


def workspace_root_arg(value: str | None) -> Path:
    return resolve_workspace_root(value, Path.cwd())


def load_registry(root: Path) -> dict:
    return read_toml(workspace_config_path(root))


def load_sources(
    root: Path,
    checkout_dir: str | None,
    *,
    invalid: dict[str, str] | None = None,
    only: str | None = None,
) -> dict:
    registry = load_registry(root)
    return resolve_sources(
        registry,
        repo_root=root,
        checkout_override=checkout_dir,
        invalid=invalid,
        only=only,
    )
