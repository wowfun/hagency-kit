from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pathspec import GitIgnoreSpec

from hagency_cli.files.cleanup.models import RuleScope


@lru_cache(maxsize=256)
def compile_patterns(patterns: tuple[str, ...]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(patterns)


def matches(
    scope: RuleScope, path: Path, is_directory: bool, *, exclude: bool = False
) -> bool:
    try:
        relative = path.relative_to(scope.root).as_posix()
    except ValueError:
        return False
    if relative == ".":
        return False
    if is_directory:
        relative += "/"
    patterns = scope.rule.exclude if exclude else scope.rule.include
    return compile_patterns(patterns).match_file(relative)


def exclusion_reason(
    scopes: tuple[RuleScope, ...], path: Path, is_directory: bool
) -> str | None:
    for scope in scopes:
        if matches(scope, path, is_directory, exclude=True):
            return f"excluded by {scope.rule.id} ({scope.rule.origin}): {path}"
    return None


def relevant_scopes(scopes: tuple[RuleScope, ...], path: Path) -> tuple[RuleScope, ...]:
    """Keep overlapping roots; unrelated projects cannot match this subtree."""
    return tuple(
        scope
        for scope in scopes
        if path.is_relative_to(scope.root) or scope.root.is_relative_to(path)
    )
