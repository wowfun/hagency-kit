from __future__ import annotations

import sys
from pathlib import Path

import questionary

from hagency_cli.commands.cleanup_render import _render_issues, format_bytes
from hagency_cli.files.cleanup.models import CleanupCandidate, CleanupPlan


class QuestionaryCleanupUI:
    """Interactive cleanup prompts backed by Questionary."""

    def __init__(self) -> None:
        self.presented_plan: CleanupPlan | None = None

    def is_interactive(self) -> bool:
        self.presented_plan = None
        return sys.stdin.isatty() and sys.stdout.isatty()

    def present_plan(self, plan: CleanupPlan) -> None:
        if plan.snapshot:
            for path, signature in plan.snapshot.files:
                print(
                    f"Rules: {path}"
                    + (" (missing; builtins apply)" if signature is None else "")
                )
        _render_issues(plan.issues)
        self.presented_plan = plan

    def select(self, choices: tuple[CleanupCandidate, ...]) -> tuple[str, ...] | None:
        prompt_choices: list[questionary.Choice | questionary.Separator] = []
        current_project: Path | None = None
        for choice in choices:
            if choice.project_path != current_project:
                current_project = choice.project_path
                prompt_choices.append(questionary.Separator(str(current_project)))
            prompt_choices.append(
                questionary.Choice(
                    title=choice.label,
                    value=choice.id,
                    checked=choice.preselected,
                )
            )
        prompt = questionary.checkbox(
            "Select files and directories to clean up",
            choices=prompt_choices,
        )
        try:
            selected = prompt.unsafe_ask()
        except EOFError:
            return None
        if selected is None:
            return None
        return tuple(selected)

    def confirm_exact(self, paths: tuple[Path, ...], known_bytes: int) -> bool:
        print("Selected paths:")
        for path in paths:
            print(f"  {path}")
        prompt = questionary.confirm(
            f"Remove {len(paths)} selected item(s) "
            f"({format_bytes(known_bytes)} known size)?",
            default=False,
        )
        try:
            confirmed = prompt.unsafe_ask()
        except EOFError:
            return False
        return bool(confirmed)


__all__ = ["QuestionaryCleanupUI"]
