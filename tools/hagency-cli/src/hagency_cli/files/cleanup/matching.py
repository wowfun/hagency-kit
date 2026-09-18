from __future__ import annotations

import os
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path

from pathspec import GitIgnoreSpec

from hagency_cli.files.cleanup.conditions import entries, policy_bindings
from hagency_cli.files.cleanup.models import RuleScope, ScanRule


@lru_cache(maxsize=256)
def compile_patterns(patterns: tuple[str, ...]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(patterns)


@lru_cache(maxsize=2048)
def _exclusion_bindings(rule: ScanRule, root: Path):
    def same_provider(entry):
        return entry.source in {"paths", rule.source} or {
            entry.source,
            rule.source,
        } <= {"project", "cachedir-tag"}

    bindings = [
        (rule, entry.root or root)
        for entry in entries(rule.match)
        if same_provider(entry)
    ]
    by_id = {policy.id: policy for policy in rule.policies}
    bindings.extend(
        (by_id[identity], base)
        for identity, expression, base in policy_bindings(rule.match, root)
        if any(same_provider(entry) for entry in entries(expression))
    )
    return tuple(dict.fromkeys(bindings))


def exclusion_reason(
    scopes: tuple[RuleScope, ...], path: Path, is_directory: bool
) -> str | None:
    for scope in scopes:
        bindings = _exclusion_bindings(scope.rule, scope.root)
        for policy, root in bindings:
            if not policy.exclude or path == root or not path.is_relative_to(root):
                continue
            relative = path.relative_to(root).as_posix() + ("/" if is_directory else "")
            if compile_patterns(policy.exclude).match_file(relative):
                return f"excluded by {policy.id} ({policy.origin}): {path}"
    return None


def relevant_scopes(scopes: tuple[RuleScope, ...], path: Path) -> tuple[RuleScope, ...]:
    """Keep overlapping roots; unrelated projects cannot match this subtree."""
    return tuple(
        scope
        for scope in scopes
        if path.is_relative_to(scope.root) or scope.root.is_relative_to(path)
    )


class ScopeIndex:
    """Planning-only lookup of overlapping roots, preserving policy order.

    Ancestors use direct lookup; descendant roots use a sorted prefix range.
    No filesystem state or deletion authorization is cached here.
    """

    def __init__(self, scopes: tuple[RuleScope, ...]):
        self.scopes = scopes
        self.by_root: dict[str, list[int]] = {}
        for index, scope in enumerate(scopes):
            self.by_root.setdefault(os.path.normcase(str(scope.root)), []).append(index)
        self.roots = sorted(self.by_root)

    def for_path(self, path: Path) -> tuple[RuleScope, ...]:
        indices = []
        for ancestor in (path, *path.parents):
            indices.extend(self.by_root.get(os.path.normcase(str(ancestor)), ()))
        prefix = os.path.normcase(str(path)).rstrip(os.sep) + os.sep
        start = bisect_left(self.roots, prefix)
        for index in range(start, len(self.roots)):
            root = self.roots[index]
            if not root.startswith(prefix):
                break
            indices.extend(self.by_root[root])
        return tuple(self.scopes[index] for index in sorted(set(indices)))
