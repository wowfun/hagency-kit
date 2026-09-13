from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from hagency_cli.files.cleanup.agents import managed_roots, protected_generic_path
from hagency_cli.files.cleanup.inspection import _valid_cachedir_tag
from hagency_cli.files.cleanup.matching import exclusion_reason, matches
from hagency_cli.files.cleanup.models import (
    Activity,
    RuleScope,
    _TreeEntry,
    _unsafe_text,
)


def included(scope: RuleScope, path: Path, is_directory: bool) -> bool:
    if scope.rule.source == "cachedir-tag" and (
        not is_directory or not _valid_cachedir_tag(path)
    ):
        return False
    return matches(scope, path, is_directory)


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
    # Descendant rules can tighten the retention of a whole-directory candidate.
    effective = set(scopes)
    for entry in tree:
        child = path / entry.relative_path
        effective.update(
            scope
            for scope in all_scopes
            if included(scope, child, stat.S_ISDIR(entry.identity.mode))
        )
    minimum = max((scope.rule.min_age_days for scope in effective), default=0)
    if any(scope.rule.source == "codex.staging" for scope in effective):
        minimum = max(7, minimum)
    preselect = max((scope.rule.preselect_after_days for scope in effective), default=0)
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
