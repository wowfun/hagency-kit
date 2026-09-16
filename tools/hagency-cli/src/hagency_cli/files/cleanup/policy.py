from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from hagency_cli.files.cleanup.agents import managed_roots, protected_generic_path
from hagency_cli.files.cleanup.conditions import evaluate, policy_bindings
from hagency_cli.files.cleanup.inspection import _valid_cachedir_tag, checkpoint
from hagency_cli.files.cleanup.matching import exclusion_reason
from hagency_cli.files.cleanup.models import (
    Activity,
    RuleScope,
    _TreeEntry,
    _unsafe_text,
)


def included(scope: RuleScope, path: Path, mode: int, size: int) -> bool:
    rule = scope.rule
    if path == scope.root or not path.is_relative_to(scope.root):
        return False
    if rule.limits and not any(
        path != limit and path.is_relative_to(limit) for limit in rule.limits
    ):
        return False
    bases = tuple(limit for limit in rule.limits if path.is_relative_to(limit)) or (
        scope.root,
    )
    return any(
        evaluate(rule.match, path, mode, size, base, rule.source, _valid_cachedir_tag)
        for base in bases
    )


def applicable_policies(scope: RuleScope, path: Path, mode: int, size: int):
    if included(scope, path, mode, size):
        yield scope.rule
    by_id = {rule.id: rule for rule in scope.rule.policies}
    for identity, expression, root in policy_bindings(scope.rule.match, scope.root):
        if path.is_relative_to(root) and evaluate(
            expression, path, mode, size, root, scope.rule.source, _valid_cachedir_tag
        ):
            yield by_id[identity]


def inspect_policy(
    scopes: tuple[RuleScope, ...], *, agent: str | None, project: bool
) -> Callable[[Path, os.stat_result], str | None]:
    protected_roots = managed_roots() if agent is None else ()

    def inspect(path: Path, info: os.stat_result) -> str | None:
        if _unsafe_text(str(path)):
            return "protected: control or bidirectional formatting characters in candidate tree"
        if path.name.startswith(".hagency-cleanup-"):
            return "protected: retained cleanup quarantine requires manual inspection"
        reason = exclusion_reason(scopes, path, stat.S_ISDIR(info.st_mode))
        if reason is None and agent is None:
            reason = protected_generic_path(
                path, project=project, roots=protected_roots
            )
        if reason is None and path.name.casefold() in {".git", ".hg", ".svn"}:
            reason = "version control metadata is protected"
        return f"protected: {reason}" if reason else None

    return inspect


def age_policy(
    path: Path,
    tree: list[_TreeEntry],
    scopes: tuple[RuleScope, ...],
    all_scopes: tuple[RuleScope, ...],
    now_ns: int,
) -> tuple[Activity, str | None]:
    # Only a larger limit can change the result. Keep checking unmatched stricter
    # policies, including references, until they match or the manifest is exhausted.
    minimum = max((scope.rule.min_age_days for scope in scopes), default=0)
    preselect = max((scope.rule.preselect_after_days for scope in scopes), default=0)
    if any(scope.rule.source == "codex.staging" for scope in scopes):
        minimum = max(7, minimum)
    pending = []
    for scope in all_scopes:
        policies = (scope.rule, *scope.rule.policies)
        upper_minimum = max(rule.min_age_days for rule in policies)
        upper_preselect = max(rule.preselect_after_days for rule in policies)
        if upper_minimum > minimum or upper_preselect > preselect:
            pending.append((scope, upper_minimum, upper_preselect))
    for entry in tree:
        if not pending:
            break
        checkpoint()
        child = path / entry.relative_path
        remaining = []
        for scope, upper_minimum, upper_preselect in pending:
            if upper_minimum <= minimum and upper_preselect <= preselect:
                continue
            for rule in applicable_policies(
                scope, child, entry.identity.mode, entry.size
            ):
                minimum = max(minimum, rule.min_age_days)
                preselect = max(preselect, rule.preselect_after_days)
            if upper_minimum > minimum or upper_preselect > preselect:
                remaining.append((scope, upper_minimum, upper_preselect))
        pending = remaining
    newest = max((entry.mtime_ns for entry in tree), default=now_ns)
    if minimum and newest >= now_ns - minimum * 86400 * 1_000_000_000:
        return (
            Activity.RECENT,
            f"protected: requires strictly more than {minimum} days without modification",
        )
    return (
        Activity.OLD
        if newest < now_ns - preselect * 86400 * 1_000_000_000
        else Activity.RECENT
    ), None
