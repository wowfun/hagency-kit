from __future__ import annotations

import os
from dataclasses import fields, replace
from pathlib import Path

from hagency_cli.files.cleanup.agents import AGENTS
from hagency_cli.files.cleanup.conditions import entries, walk
from hagency_cli.files.cleanup.models import (
    CLEANUP_COMMANDS,
    CleanRequest,
    CleanupRule,
    PurgeRequest,
    RulesSnapshot,
    ScanRule,
)


class CleanupSelectionError(ValueError):
    """Invalid invocation, rejected before inspecting cleanup targets."""


def select_rules(
    request: CleanRequest | PurgeRequest, snapshot: RulesSnapshot
) -> tuple[CleanupRule, ...]:
    if type(request.yes) is not bool:
        raise CleanupSelectionError("yes must be boolean")
    if type(request.scan_timeout) is not int or request.scan_timeout < 0:
        raise CleanupSelectionError("scan_timeout must be a nonnegative integer")
    if (
        type(request.recursive) is not bool
        or type(request.include_unverified) is not bool
    ):
        raise CleanupSelectionError("recursive and include_unverified must be boolean")
    command = "clean" if isinstance(request, CleanRequest) else "purge"
    if isinstance(request, CleanRequest):
        if request.agents and request.rules:
            raise CleanupSelectionError("--agent and --rule are mutually exclusive")
        if any(agent not in AGENTS for agent in request.agents):
            raise CleanupSelectionError("unknown cleanup agent")

    by_id = {rule.id: rule for rule in snapshot.rules}
    selected: set[str] = set()
    for value in request.rules or ("auto",):
        if value == "auto":
            selected.update(
                rule.id
                for rule in snapshot.rules
                if rule.command == command and rule.enabled and rule.auto_apply
            )
            continue
        identity = (
            value
            if any(value.startswith(f"{name}.") for name in CLEANUP_COMMANDS)
            else f"{command}.{value}"
        )
        if not identity.startswith(f"{command}."):
            raise CleanupSelectionError(f"rule {value!r} does not belong to {command}")
        rule = by_id.get(identity)
        if rule is None:
            raise CleanupSelectionError(f"unknown cleanup rule: {identity!r}")
        if not rule.enabled:
            raise CleanupSelectionError(f"cleanup rule is disabled: {identity}")
        selected.add(identity)

    rules = tuple(rule for rule in snapshot.rules if rule.id in selected)
    expressions = snapshot.expressions
    compiled = []
    paths = tuple(Path(os.path.abspath(path.expanduser())) for path in request.paths)
    for rule in rules:
        expression = expressions[rule.id]
        referenced = tuple(
            dict.fromkeys(node.value for node in walk(expression) if node.op == "ref")
        )
        for identity in referenced:
            if not by_id[identity].enabled:
                raise CleanupSelectionError(
                    f"cleanup rule is disabled: {identity} (referenced by {rule.id})"
                )
        for entry in entries(expression):
            source = entry.source
            if isinstance(request, PurgeRequest) and source == "paths":
                source = "project"
            if (
                isinstance(request, CleanRequest)
                and request.agents
                and "." in source
                and source.split(".")[0] not in request.agents
            ):
                continue
            if (
                isinstance(request, CleanRequest)
                and source in {"paths", "project", "cachedir-tag"}
                and entry.root is None
                and not paths
            ):
                raise CleanupSelectionError(
                    f"selected rules require PATH or a within condition: {rule.id}"
                )
            values = {
                field.name: getattr(rule, field.name) for field in fields(CleanupRule)
            }
            values["match"] = expression
            compiled.append(
                ScanRule(
                    **values,
                    source=source,
                    root=entry.root,
                    limits=paths,
                    policies=tuple(
                        replace(by_id[identity], match=expressions[identity])
                        for identity in referenced
                    ),
                )
            )
    return tuple(dict.fromkeys(compiled))
