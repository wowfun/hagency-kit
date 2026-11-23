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
    """Create a link with -s. DEST defaults to the current directory. A directory
    destination receives a link named after TARGET. Relative targets resolve
    from the link's parent directory. Windows uses junctions for directories
    and requires the target to exist.

    \b
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
