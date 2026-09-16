"""Immutable cleanup expressions, entry planning and metadata-only evaluation.

Each load_rules call expands references once into RulesSnapshot.expressions.
Global LRU caches are bounded and contain only pure results keyed by immutable,
content-hashed conditions. Evaluation memoization is local to a single evaluate
call: path, metadata, source and cachedir callback stay constant while only the
expression and lexical root vary. Never reuse that memo across candidates or
between planning and deletion revalidation.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from pathspec import GitIgnoreSpec

from hagency_cli.files.cleanup.file_filters import (
    is_image_name,
    is_text_name,
    is_video_name,
    parse_file_size,
)

SOURCES = frozenset(
    {
        "project",
        "cachedir-tag",
        "codex.versions",
        "claude.versions",
        "copilot.versions",
        "codex.staging",
    }
)


@dataclass(frozen=True, eq=False)
class Condition:
    op: str
    value: str | bool | Path | tuple[int | None, int | None] | None = None
    children: tuple[Condition, ...] = ()

    _hash: int = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.op in {"all", "any"}:
            if self.value is not None or not self.children:
                raise ValueError("empty or malformed boolean condition")
        elif self.op == "not":
            if self.value is not None or len(self.children) != 1:
                raise ValueError("not requires one condition")
        elif self.op == "ref":
            if not isinstance(self.value, str) or len(self.children) > 1:
                raise ValueError("invalid ref condition")
        else:
            if self.children:
                raise ValueError("leaf conditions cannot have children")
            if self.op in {"is_text", "is_image", "is_video"}:
                if type(self.value) is not bool:
                    raise ValueError(f"{self.op} must be boolean")
            elif self.op == "size":
                if (
                    not isinstance(self.value, tuple)
                    or len(self.value) != 2
                    or all(v is None for v in self.value)
                ):
                    raise ValueError("size requires bounds")
                for bound in self.value:
                    if bound is not None and (
                        type(bound) is not int or not 0 <= bound <= 2**63 - 1
                    ):
                        raise ValueError("invalid canonical byte bound")
                low, high = self.value
                if low is not None and high is not None and low > high:
                    raise ValueError("min_file_size must not exceed max_file_size")
            elif self.op == "within":
                if not isinstance(self.value, Path) or not self.value.is_absolute():
                    raise ValueError("within requires an absolute parsed path")
            elif self.op in {"glob", "source"}:
                if not isinstance(self.value, str) or not self.value:
                    raise ValueError(f"invalid {self.op} condition")
            else:
                raise ValueError(f"unknown condition: {self.op}")
        object.__setattr__(self, "_hash", hash((self.op, self.value, self.children)))

    def __hash__(self):
        return self._hash

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, Condition) or self._hash != other._hash:
            return False
        # Shared reference graphs must compare in graph size, not expanded-tree size.
        pending, seen = [(self, other)], set()
        while pending:
            left, right = pending.pop()
            key = (id(left), id(right))
            if left is right or key in seen:
                continue
            seen.add(key)
            if (
                left.op != right.op
                or left.value != right.value
                or len(left.children) != len(right.children)
            ):
                return False
            pending.extend(zip(left.children, right.children))
        return True

    def data(self) -> dict:
        if self.op in {"all", "any"}:
            return {self.op: [child.data() for child in self.children]}
        if self.op == "not":
            return {"not": self.children[0].data()}
        if self.op == "size":
            return {
                key: value
                for key, value in zip(("min_file_size", "max_file_size"), self.value)
                if value is not None
            }
        return {
            self.op: str(self.value) if isinstance(self.value, Path) else self.value
        }


@lru_cache(maxsize=1024)
def pattern(value: str) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines([value])


def parse_condition(
    value: object, base: Path, command: str, depth: int = 0
) -> Condition:
    if depth > 64:
        raise ValueError("match nesting exceeds 64 levels")
    if not isinstance(value, dict) or not value:
        raise ValueError("match must be a nonempty condition table")
    if set(value) <= {"min_file_size", "max_file_size"}:
        bounds = tuple(
            parse_file_size(value[key]) if key in value else None
            for key in ("min_file_size", "max_file_size")
        )
        if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
            raise ValueError("min_file_size must not exceed max_file_size")
        return Condition("size", bounds)
    if len(value) != 1:
        raise ValueError("each condition must contain exactly one operator")
    op, operand = next(iter(value.items()))
    if op in {"all", "any", "not"}:
        operands = [operand] if op == "not" else operand
        if not isinstance(operands, list) or not operands:
            raise ValueError(f"{op} requires a nonempty condition array")
        return Condition(
            op,
            children=tuple(
                parse_condition(item, base, command, depth + 1) for item in operands
            ),
        )
    if op in {"is_text", "is_image", "is_video"}:
        if type(operand) is not bool:
            raise ValueError(f"{op} must be boolean")
    elif op in {"within", "glob", "ref", "source"}:
        if (
            not isinstance(operand, str)
            or not operand.strip()
            or any(
                ord(c) < 32
                or 127 <= ord(c) <= 159
                or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
                for c in operand
            )
        ):
            raise ValueError(f"invalid {op} condition")
        if op == "within":
            from hagency_cli.paths import expand_path

            operand = Path(os.path.abspath(expand_path(operand, base)))
        elif op == "glob":
            pattern(operand)
        elif op == "source":
            if operand not in SOURCES or command != (
                "purge" if operand in {"project", "cachedir-tag"} else "clean"
            ):
                raise ValueError(f"invalid command/source: {operand}")
        elif not operand.startswith(command + "."):
            raise ValueError(f"ref requires a full {command} rule ID")
    else:
        raise ValueError(f"unknown match condition: {op}")
    return Condition(op, operand)


def walk(node: Condition):
    pending, seen = [node], set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yield current
        pending.extend(reversed(current.children))


@lru_cache(maxsize=2048)
def condition_depth(node: Condition) -> int:
    return 1 + max((condition_depth(child) for child in node.children), default=0)


def expand_rules(rules):
    """Resolve the merged graph once; shared references remain shared nodes."""
    by_id = {rule.id: rule for rule in rules}
    resolved = {}
    visiting = set()

    def resolve(identity):
        if identity in resolved:
            return resolved[identity]
        if identity not in by_id:
            raise ValueError(f"unknown rule reference: {identity}")
        if len(visiting) >= 64:
            raise ValueError("rule reference nesting exceeds 64 levels")
        if identity in visiting:
            raise ValueError(f"cyclic rule reference: {identity}")
        visiting.add(identity)
        for node in walk(by_id[identity].match):
            if node.op == "ref":
                resolve(node.value)

        def expand(node):
            if node.op == "ref":
                return Condition("ref", node.value, (resolved[node.value],))
            return Condition(
                node.op, node.value, tuple(expand(child) for child in node.children)
            )

        resolved[identity] = expand(by_id[identity].match)
        if condition_depth(resolved[identity]) > 64:
            raise ValueError(f"expanded match nesting exceeds 64 levels: {identity}")
        visiting.remove(identity)
        return resolved[identity]

    for identity in by_id:
        resolve(identity)
    return resolved


@dataclass(frozen=True)
class Entry:
    source: str
    root: Path | None
    required: bool = False


@lru_cache(maxsize=2048)
def local_entry(node: Condition) -> tuple[Path | None, str | None]:
    """Conjunctive siblings establish one lexical glob base and provider."""
    if node.op == "within":
        return node.value, None
    if node.op == "source":
        return None, node.value
    if node.op == "ref":
        return local_entry(node.children[0])
    if node.op != "all":
        return None, None
    roots, sources = set(), set()
    for child in node.children:
        root, source = local_entry(child)
        if root is not None:
            roots.add(root)
        if source is not None:
            sources.add(source)
    if sources == {"project", "cachedir-tag"}:
        sources = {"project"}
    if len(roots) > 1 or len(sources) > 1:
        raise ValueError(
            "ambiguous entry in all condition; put distinct entries in any branches"
        )
    return next(iter(roots), None), next(iter(sources), None)


@lru_cache(maxsize=2048)
def unbound_glob(node: Condition) -> bool:
    if local_entry(node)[0] is not None:
        return False
    return node.op == "glob" or any(unbound_glob(child) for child in node.children)


@lru_cache(maxsize=2048)
def entries(
    node: Condition, root: Path | None = None, source: str = "paths"
) -> tuple[Entry, ...]:
    own_root, own_source = local_entry(node)
    root, source = own_root or root, own_source or source
    if node.op in {"all", "any", "ref"}:
        result = tuple(
            dict.fromkeys(
                entry
                for child in node.children
                for entry in entries(child, root, source)
            )
        )
        if node.op == "any":
            result = tuple(
                replace(entry, required=True) if entry.root is None else entry
                for entry in result
            )
        # An independent unanchored any branch still requires a fallback entry.
        if node.op == "all":
            rooted_sources = {
                entry.source for entry in result if entry.root is not None
            }
            has_entry = any(
                entry.root is not None or entry.source != "paths" for entry in result
            )
            # Keep required independent any branches and explicit roots.
            # Without another entry, keep the CLI fallback. A specialized
            # provider also survives unless that same source has a rooted entry.
            # Other rootless conjuncts inherit an entry instead of adding a scan.
            result = tuple(
                entry
                for entry in result
                if entry.required
                or entry.root is not None
                or not has_entry
                or (entry.source != "paths" and entry.source not in rooted_sources)
            )
            distinct_roots = {entry.root for entry in result if entry.root is not None}
            if (
                own_root is None
                and len(distinct_roots) > 1
                and any(unbound_glob(child) for child in node.children)
            ):
                raise ValueError("ambiguous glob base across entry branches")
        return result
    return (Entry(source, root),)


@lru_cache(maxsize=2048)
def kinds(node: Condition) -> frozenset[str]:
    if node.op in {"size", "is_text", "is_image", "is_video"}:
        return frozenset({"file"})
    if node.op in {"not", "ref"}:
        return kinds(node.children[0])
    if node.op in {"all", "any"}:
        combine = frozenset.intersection if node.op == "all" else frozenset.union
        return combine(*(kinds(child) for child in node.children))
    return frozenset({"file", "directory"})


def evaluate(
    node: Condition,
    path: Path,
    mode: int,
    size: int,
    root: Path,
    source: str,
    cachedir: Callable[[Path], bool],
    memo: dict[tuple[str, Path], bool] | None = None,
) -> bool:
    if memo is None:
        memo = {}
    kind = (
        "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"
    )
    if kind not in kinds(node):
        return False
    own_root, _ = local_entry(node)
    root = own_root or root
    if node.op in {"all", "any"}:
        combine = all if node.op == "all" else any
        return combine(
            evaluate(child, path, mode, size, root, source, cachedir, memo)
            for child in node.children
        )
    if node.op == "not":
        return not evaluate(
            node.children[0], path, mode, size, root, source, cachedir, memo
        )
    if node.op == "ref":
        key = (node.value, root)
        if key not in memo:
            memo[key] = evaluate(
                node.children[0], path, mode, size, root, source, cachedir, memo
            )
        return memo[key]
    if node.op == "within":
        return path != node.value and path.is_relative_to(node.value)
    if node.op == "glob":
        if path == root or not path.is_relative_to(root):
            return False
        relative = path.relative_to(root).as_posix() + (
            "/" if kind == "directory" else ""
        )
        return pattern(node.value).match_file(relative)
    if node.op == "source":
        if node.value == "cachedir-tag":
            return (
                source in {"project", "cachedir-tag"}
                and kind == "directory"
                and cachedir(path)
            )
        return node.value == source or (
            node.value == "project" and source == "cachedir-tag"
        )
    if node.op == "size":
        minimum, maximum = node.value
        return (minimum is None or size >= minimum) and (
            maximum is None or size <= maximum
        )
    classify = {
        "is_text": is_text_name,
        "is_image": is_image_name,
        "is_video": is_video_name,
    }
    return classify[node.op](path) == node.value


@lru_cache(maxsize=2048)
def policy_bindings(
    node: Condition, root: Path
) -> tuple[tuple[str, Condition, Path], ...]:
    own_root, _ = local_entry(node)
    root = own_root or root
    result = [(node.value, node.children[0], root)] if node.op == "ref" else []
    for child in node.children:
        result.extend(policy_bindings(child, root))
    return tuple(dict.fromkeys(result))
