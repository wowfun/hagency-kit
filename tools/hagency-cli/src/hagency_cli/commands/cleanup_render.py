from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import fields
from typing import Any

from hagency_cli.files.cleanup.conditions import Condition, entries, walk
from hagency_cli.files.cleanup.models import (
    CleanupPlan,
    CleanupReport,
    CleanupRule,
    PathsEditReport,
    _display_text,
)
from hagency_cli.files.cleanup.models import (
    _format_bytes as format_bytes,
)


def render_cleanup_rules(
    rules: tuple[CleanupRule, ...],
    *,
    detail: bool = False,
    resolved: Mapping[str, Condition] | None = None,
) -> None:
    def sources(rule: CleanupRule) -> str:
        expression = resolved[rule.id] if resolved is not None else rule.match
        if resolved is not None or not any(
            node.op == "ref" for node in walk(expression)
        ):
            return ", ".join(
                dict.fromkeys(
                    "project"
                    if entry.source == "paths" and rule.command == "purge"
                    else entry.source
                    for entry in entries(expression)
                )
            )
        return (
            ", ".join(
                dict.fromkeys(
                    str(node.value) for node in walk(expression) if node.op == "source"
                )
            )
            or "paths"
        )

    if not rules:
        print("No cleanup rules found.")
        return
    if detail:
        for index, rule in enumerate(rules):
            if index:
                print()
            for field in fields(rule):
                value = (
                    rule.match.data()
                    if field.name == "match"
                    else getattr(rule, field.name)
                )
                print(
                    _display_text(
                        f"{field.name} = {json.dumps(value, ensure_ascii=False, default=str)}"
                    )
                )
            print(f"sources = {json.dumps(sources(rule).split(', '))}")
            print(
                f"references = {json.dumps(list(dict.fromkeys(node.value for node in walk(rule.match) if node.op == 'ref')))}"
            )
        return
    print("ID | STATE | SOURCE | ORIGIN")
    for rule in rules:
        state = (
            "disabled"
            if not rule.enabled
            else "auto"
            if rule.auto_apply
            else "explicit-only"
        )
        print(
            " | ".join(
                _display_text(cell).replace("|", r"\|")
                for cell in (rule.id, state, sources(rule), rule.origin)
            )
        )


def _value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _render_issues(issues: Iterable[Any]) -> None:
    for issue in issues:
        level = "Error" if issue.is_failure else "Warning"
        code = f" [{issue.code}]" if issue.code else ""
        path = f" ({issue.path})" if issue.path is not None else ""
        print(_display_text(f"{level}{code}{path}: {issue.message}"), file=sys.stderr)


def _selected_unknown_count(report: CleanupReport) -> int:
    selected = set(report.selected_paths)
    return sum(
        choice.exact_path in selected and choice.size_bytes is None
        for choice in report.choices
    )


def _size_summary(known_bytes: int, unknown_count: int) -> str:
    summary = f"known size {format_bytes(known_bytes)}"
    if unknown_count:
        suffix = "artifact" if unknown_count == 1 else "artifacts"
        summary += f", {unknown_count} {suffix} with unknown size"
    return summary


def _render_choices(report: CleanupReport) -> None:
    selected = set(report.selected_paths)
    for choice in report.choices:
        marker = "[selected]" if choice.exact_path in selected else "[ ]"
        activity = _value(choice.activity).replace("_", " ")
        size = (
            format_bytes(choice.size_bytes)
            if choice.size_bytes is not None
            else "size unknown"
        )
        print(
            f"{marker} {choice.group} | {activity} | {choice.entry_kind} | {choice.artifact_kind} | {size} | "
            f"{choice.exact_path} | rules: {', '.join(choice.rule_ids)} | config: {', '.join(choice.rule_origins)}"
            + (f" | {choice.verification_note}" if choice.verification_note else "")
        )


def _result_prefix(disposition: Any) -> str:
    value = _value(disposition)
    if value in {"preview", "would_remove", "would-remove"}:
        return "Would remove"
    if value in {"removed", "completed"}:
        return "removed"
    if value in {"skipped", "cancelled", "canceled"}:
        return "skipped"
    return "failed"


def _render_results(report: CleanupReport) -> None:
    for result in report.results:
        prefix = _result_prefix(result.disposition)
        size = (
            f" ({format_bytes(result.size_bytes)})"
            if result.size_bytes is not None
            else ""
        )
        message = f" - {result.message}" if result.message else ""
        output = sys.stderr if prefix == "failed" else sys.stdout
        print(
            _display_text(f"{prefix}: {result.exact_path}{size}{message}"), file=output
        )


def render_cleanup_report(
    report: CleanupReport, *, presented_plan: CleanupPlan | None = None
) -> None:
    shown_issues = presented_plan.issues if presented_plan else ()
    shown_files = (
        {path for path, _signature in presented_plan.snapshot.files}
        if presented_plan and presented_plan.snapshot
        else set()
    )
    _render_issues(issue for issue in report.issues if issue not in shown_issues)
    action = report.command.capitalize()
    for path in report.config_files:
        if path not in shown_files:
            print(f"Rules: {path}")

    if report.choices:
        _render_choices(report)
    _render_results(report)

    disposition = _value(report.disposition)
    if disposition in {"cancelled", "canceled"}:
        print(f"{action} cancelled.")
        return
    if not report.choices:
        if report.failed:
            print(f"{action} incomplete; see reported errors.", file=sys.stderr)
        else:
            print(f"No {report.command} candidates found.")
        return

    selected_count = len(report.selected_paths)
    size = _size_summary(report.known_bytes, _selected_unknown_count(report))
    if disposition == "preview":
        state = "incomplete" if report.failed else "complete"
        print(f"Preview {state}: {selected_count} artifact(s), {size}.")
    elif disposition == "partial":
        print(
            f"{action} partially completed: {selected_count} selected artifact(s), {size}."
        )
    else:
        print(f"{action} complete: {selected_count} selected artifact(s), {size}.")


def render_paths_edit_report(report: PathsEditReport) -> None:
    _render_issues(report.issues)
    if report.failed:
        print(f"Failed to update purge paths: {report.config_path}", file=sys.stderr)
    else:
        action = "unchanged" if report.before_roots == report.after_roots else "updated"
        print(f"Purge paths {action}: {report.config_path}")
        if report.editor:
            print(f"Editor: {report.editor}")

    for label, roots in (
        ("Before", report.before_roots),
        ("After", report.after_roots),
    ):
        print(f"{label}:")
        if not roots:
            print("  (none)")
        for root in roots:
            print(f"  {root}")


__all__ = ["render_cleanup_report", "render_cleanup_rules", "render_paths_edit_report"]
