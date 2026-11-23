from typing import Annotated

import typer

from hagency_cli.commands.completion import complete_directory

WorkspaceRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help=(
            "Workspace root. By default, search the current directory and its parents, "
            "then the editable checkout."
        ),
        autocompletion=complete_directory,
    ),
]
