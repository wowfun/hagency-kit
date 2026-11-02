from __future__ import annotations

import inspect
import io
import os
import shlex
import sys
from functools import wraps
from typing import get_type_hints

import typer

# Typer 0.27 vendors Click and raises this exception even without a top-level click package.
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

from hagency_cli.workspace.errors import (
    SkillNameConflictError,
    SkillReferenceError,
    SkillSymlinkError,
    SourceBatchError,
    SourceNotReadyError,
    WorkspaceError,
)
from hagency_cli.workspace.events import OperationEvent, Severity


def diagnostic(message: str, severity: Severity) -> None:
    color = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    typer.secho(
        message,
        file=sys.stderr,
        fg="yellow" if severity == "warning" else "red",
        color=color,
    )


def die(message: str) -> None:
    diagnostic(f"Error: {message}", "error")
    raise typer.Exit(1)


def render_event(event: OperationEvent) -> None:
    if event.severity:
        diagnostic(event.message, event.severity)
    else:
        print(event.message, file=sys.stderr if event.stderr else sys.stdout)


def command_errors(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkspaceError as exc:
            values = inspect.signature(function).bind(*args, **kwargs).arguments
            die(render_workspace_error(exc, function.__name__, values))

    invoke.__annotations__ = get_type_hints(function, include_extras=True)
    return invoke


def require_at_most_one(options: dict[str, object]) -> None:
    selected = [name for name, value in options.items() if value not in (None, False)]
    if len(selected) > 1:
        raise typer.BadParameter(
            f"options are mutually exclusive: {', '.join(selected)}"
        )


def require_exactly_one(options: dict[str, object]) -> None:
    require_at_most_one(options)
    if not any(value not in (None, False) for value in options.values()):
        raise typer.BadParameter(
            f"one of the options is required: {', '.join(options)}"
        )


class CommandGroup(TyperGroup):
    """Group existing command aliases in help without hiding them from completion."""

    aliases = {
        "s": "source",
        "p": "profile",
        "ls": "list",
        "rm": "remove",
        "u": "update",
    }

    def main(self, *args, standalone_mode=True, **kwargs):
        # Let Typer format usage and hints, styling only the diagnostic itself.
        try:
            result = super().main(*args, standalone_mode=False, **kwargs)
        except ClickException as exc:
            if not standalone_mode:
                raise
            output = io.StringIO()
            exc.show(file=output)
            rendered = output.getvalue()
            message = f"Error: {exc.format_message()}"
            if rendered.endswith(message + "\n"):
                sys.stderr.write(rendered.removesuffix(message + "\n"))
                diagnostic(message, "error")
            else:
                sys.stderr.write(rendered)
            raise SystemExit(exc.exit_code) from None
        except typer.Abort:
            if not standalone_mode:
                raise
            diagnostic("Aborted!", "error")
            raise SystemExit(1) from None
        if standalone_mode:
            raise SystemExit(result if isinstance(result, int) else 0)
        return result

    def format_commands(self, ctx, formatter) -> None:
        commands = {
            name: command
            for name in self.list_commands(ctx)
            if (command := self.get_command(ctx, name)) is not None
            and not command.hidden
        }
        aliases = {
            alias: name
            for alias, name in self.aliases.items()
            if alias in commands and name in commands
        }
        rows = []
        for name, command in commands.items():
            if name in aliases:
                continue
            label = ", ".join(
                [name, *(alias for alias, target in aliases.items() if target == name)]
            )
            rows.append((label, command))
        if rows:
            limit = formatter.width - 6 - max(len(label) for label, _ in rows)
            with formatter.section("Commands"):
                formatter.write_dl(
                    [
                        (label, command.get_short_help_str(limit))
                        for label, command in rows
                    ]
                )


def make_app(*, help_text: str, add_completion: bool) -> typer.Typer:
    return typer.Typer(
        cls=CommandGroup,
        help=help_text,
        add_completion=add_completion,
        context_settings={"help_option_names": ["-h", "--help"]},
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        no_args_is_help=False,
    )


def render_workspace_error(error: WorkspaceError, command: str, values: dict) -> str:
    if isinstance(error, SkillNameConflictError):
        hint = (
            "use an exact --skill selector"
            if command == "skill_add_cli"
            else "narrow the profile's include selector"
        )
        return f"{error}; rerun in an interactive terminal to choose one or {hint}"
    if isinstance(error, SkillSymlinkError) and error.windows:
        hint = "; on Windows, rerun PowerShell or Git Bash as Administrator"
        if command == "profile_apply_cli":
            hint += ", use --link-mode junction, or use -cp"
        return str(error) + hint
    if isinstance(error, SkillReferenceError):
        if command == "skill_add_cli":
            prefix = ["hgc", "skill", "add"]
        else:
            action = "add" if command == "profile_add_cli" else "update"
            option = "-RS" if values.get("remove_skill") == error.reference else "-AS"
            prefix = ["hgc", "profile", action, values["name"], option]
        return (
            error.message
            + "\n"
            + "\n".join("  " + shlex.join([*prefix, ref]) for ref in error.references)
        )
    if isinstance(error, SourceNotReadyError):
        return (
            str(error)
            + "; run: "
            + shlex.join(["hgc", "source", "sync", *error.sources])
        )
    if isinstance(error, SourceBatchError) and error.reanchor:
        return (
            str(error)
            + "\nTip: if these checkouts are disposable and local-only commits may be discarded, run:\n  "
            + shlex.join(["hgc", "source", "sync", *error.reanchor, "--reanchor"])
        )
    return str(error)
