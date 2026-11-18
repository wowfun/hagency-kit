from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path


def _xdg_directory(
    variable: str, default: Path, environ: Mapping[str, str] | None
) -> Path:
    environ = os.environ if environ is None else environ
    value = environ.get(variable)
    base = Path(value) if value else default
    if not base.is_absolute():
        base = default
    return base / "hagency"


def user_config_dir(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    home = Path.home() if home is None else home
    return _xdg_directory("XDG_CONFIG_HOME", home / ".config", environ)


def user_state_dir(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    home = Path.home() if home is None else home
    return _xdg_directory("XDG_STATE_HOME", home / ".local" / "state", environ)


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
