from __future__ import annotations

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
