from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.shared import die
from hagency_cli.files.links import create_symbolic_link
from hagency_cli.workspace.errors import format_called_process_error


def ln_cli(
    target: Annotated[
        str, typer.Argument(metavar="TARGET", help="File or directory the link points to")
    ],
    dest: Annotated[
        str, typer.Argument(metavar="[DEST]", help="Link name or existing directory")
    ] = ".",
    symbolic: Annotated[
        bool,
        typer.Option("--symbolic", "-s", help="Create a symbolic link"),
    ] = False,
) -> None:
    """Create a link to a file or directory.

    DEST defaults to the current directory. An existing destination directory
    receives a link named after the target.
    Relative targets are interpreted from the final link's parent directory.
    The parent must exist and the final link path must be free.

    On native Windows, including Git Bash, directory links are PowerShell
    junctions and the target must exist. File symlinks may require Windows
    Developer Mode or an elevated shell. Linux, macOS, and WSL allow dangling
    symlinks. No Hagency workspace is needed.

    \b
    Examples:
      hgc ln -s ./shared/assets
      hgc ln -s ./assets ./assets-link
      hgc ln -s ../assets ./public
    """
    if not symbolic:
        raise typer.BadParameter("specify -s or --symbolic")
    if not target or not dest:
        raise typer.BadParameter("TARGET and DEST must be nonempty")

    try:
        create_symbolic_link(target, dest, cwd=Path.cwd())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = (
            format_called_process_error(exc)
            if isinstance(exc, subprocess.CalledProcessError)
            else str(exc)
        )
        if getattr(exc, "winerror", None) == 1314:
            detail += "\nEnable Windows Developer Mode or run the shell as Administrator."
        die(f"could not create link {dest} -> {target}: {detail}")
