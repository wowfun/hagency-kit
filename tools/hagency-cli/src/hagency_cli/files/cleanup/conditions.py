"""Immutable cleanup expressions, entry planning and candidate evaluation.

Each load_rules call expands references once into RulesSnapshot.expressions.
Global LRU caches are bounded and contain only pure results keyed by immutable,
content-hashed conditions. Evaluation memoization is local to a single evaluate
call: path, metadata, source and cachedir callback stay constant while only the
expression and lexical root vary. Never reuse that memo across candidates or
between planning and deletion revalidation.
"""

from __future__ import annotations

import json
import operator
import os
import re
import stat
import tomllib
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
COMPARISONS = {
    "<": operator.lt,
    "<=": operator.le,
    "=": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    ">": operator.gt,
}
BOOLEAN_CONDITIONS = frozenset(
    {"is_text", "is_image", "is_video", "is_empty_directory"}
)


@dataclass(frozen=True, eq=False)
class Condition:
    op: str
    value: str | bool | Path | tuple[str, int] | None = None
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
            if self.op in BOOLEAN_CONDITIONS:
                if type(self.value) is not bool:
                    raise ValueError(f"{self.op} must be boolean")
            elif self.op == "file_size":
                if (
                    not isinstance(self.value, tuple)
                    or len(self.value) != 2
                    or self.value[0] not in COMPARISONS
                    or type(self.value[1]) is not int
                    or not 0 <= self.value[1] <= 2**63 - 1
                ):
                    raise ValueError(
                        "file_size requires a comparison and canonical bytes"
                    )
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

    def expression(self) -> str:
        """Render the declared tree, retaining lexical groups and reference IDs."""

        def grouped(child: Condition) -> str:
            value = child.expression()
            return f"({value})" if child.op in {"all", "any"} else value

        if self.op in {"all", "any"}:
            separator = " & " if self.op == "all" else " | "
            return separator.join(grouped(child) for child in self.children)
        if self.op == "not":
            return "!" + grouped(self.children[0])
        if self.op == "file_size":
            comparison, size = self.value
            return f"file_size {comparison} {size}"
        if self.op in BOOLEAN_CONDITIONS:
            return self.op if self.value else "!" + self.op
        value = str(self.value) if isinstance(self.value, Path) else self.value
        return f"{self.op}={json.dumps(value, ensure_ascii=False)}"


@lru_cache(maxsize=1024)
def pattern(value: str) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines([value])


def _leaf_condition(op: str, operand: object, base: Path, command: str) -> Condition:
    if op in BOOLEAN_CONDITIONS:
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
        elif not operand.startswith(
            ("clean.", "purge.") if command == "clean" else ("purge.",)
        ):
            allowed = "clean. or purge." if command == "clean" else "purge."
            raise ValueError(
                f"ref requires a full rule ID starting with {allowed}: {operand}"
            )
    else:
        raise ValueError(f"unknown match condition: {op}")
    return Condition(op, operand)


_TOKEN = re.compile(
    r"""(?P<space>[ \t\r\n]+)|(?P<string>"(?:[^"\\\r\n]|\\[^\r\n])*"|'[^'\r\n]*')"""
    r"|(?P<symbol><=|>=|!=|[<>=!&|()])"
    r"|(?P<name>[a-zA-Z_][a-zA-Z_0-9]*)"
    r"|(?P<number>[0-9]+(?:\.[0-9]+)?(?:[ \t]*[a-zA-Z]+)?)"
)


class _ExpressionParser:
    """Streaming lexer and recursive descent; boolean chains do not recurse."""

    def __init__(self, text: str, base: Path, command: str):
        self.text, self.base, self.command = text, base, command
        self.position = 0
        self.advance()

    def error(self, message: str, position: int | None = None) -> ValueError:
        position = self.start if position is None else position
        line = self.text.count("\n", 0, position) + 1
        column = position - self.text.rfind("\n", 0, position)
        return ValueError(f"match expression line {line}, column {column}: {message}")

    def advance(self) -> None:
        while self.position < len(self.text):
            self.start = self.position
            token = _TOKEN.match(self.text, self.position)
            if token is None:
                raise self.error("invalid token or unterminated string")
            self.position = token.end()
            if token.lastgroup != "space":
                self.kind, self.token = token.lastgroup, token.group()
                return
        self.start = self.position
        self.kind, self.token = "end", ""

    def take(self, token: str) -> bool:
        if self.token != token:
            return False
        self.advance()
        return True

    def parse(self) -> Condition:
        result = self.disjunction(0)
        if self.kind != "end":
            raise self.error("expected '&', '|' or end of expression")
        if condition_depth(result) > 64:
            raise self.error("match nesting exceeds 64 levels")

        def canonical(node: Condition) -> Condition:
            if not node.children:
                return node
            children = tuple(canonical(child) for child in node.children)
            if node.op == "not" and children[0].op in BOOLEAN_CONDITIONS:
                return Condition(children[0].op, not children[0].value)
            return replace(node, children=children)

        # Fold boolean negation only after validating the original nesting.
        # This also preserves false predicates for consumers such as cascade planning.
        return canonical(result)

    def disjunction(self, depth: int) -> Condition:
        children = [self.conjunction(depth)]
        while self.take("|"):
            children.append(self.conjunction(depth))
        return (
            children[0]
            if len(children) == 1
            else Condition("any", children=tuple(children))
        )

    def conjunction(self, depth: int) -> Condition:
        children = [self.unary(depth)]
        while self.take("&"):
            children.append(self.unary(depth))
        return (
            children[0]
            if len(children) == 1
            else Condition("all", children=tuple(children))
        )

    def unary(self, depth: int) -> Condition:
        if depth > 64:
            raise self.error("match nesting exceeds 64 levels")
        if self.take("!"):
            return Condition("not", children=(self.unary(depth + 1),))
        if self.take("("):
            result = self.disjunction(depth + 1)
            if not self.take(")"):
                raise self.error("expected ')'")
            return result
        return self.leaf()

    def leaf(self) -> Condition:
        if self.kind != "name":
            raise self.error("expected a condition or '('")
        op = self.token
        if op in {"min_file_size", "max_file_size"}:
            raise self.error(
                "use file_size comparisons instead of min_file_size/max_file_size"
            )
        self.advance()
        comparison = self.token
        if op in BOOLEAN_CONDITIONS and comparison in {"", "&", "|", ")"}:
            return Condition(op, True)
        if comparison not in COMPARISONS or (op != "file_size" and comparison != "="):
            raise self.error(
                "expected a file_size comparison"
                if op == "file_size"
                else "expected '='"
            )
        self.advance()
        kind, raw, position = self.kind, self.token, self.start
        if kind not in {"string", "number", "name"}:
            raise self.error("expected a condition value")
        try:
            if kind == "string":
                value = tomllib.loads("value=" + raw)["value"]
            elif raw in {"true", "false"}:
                value = raw == "true"
            elif op == "file_size" and kind == "number":
                # Route integers through the bounded unit parser too, including
                # arbitrarily long zero padding without Python's int digit limit.
                value = raw + "B" if raw.isdigit() else raw
            else:
                raise ValueError("expected a quoted string, boolean or file size")
            if op == "file_size":
                result = Condition(op, (comparison, parse_file_size(value)))
            else:
                result = _leaf_condition(op, value, self.base, self.command)
        except (ValueError, TypeError) as exc:
            raise self.error(str(exc), position) from exc
        self.advance()
        return result


def parse_condition(value: object, base: Path, command: str) -> Condition:
    if not isinstance(value, str):
        raise TypeError(
            "match expression line 1, column 1: match must be an expression string; "
            "condition tables are no longer supported"
        )
    return _ExpressionParser(value, base, command).parse()


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


def has_positive_empty_directory(node: Condition) -> bool:
    """Find an emptiness predicate, accounting for negated groups and references."""
    pending, seen = [(node, False)], set()
    while pending:
        current, negated = pending.pop()
        key = (id(current), negated)
        if key in seen:
            continue
        seen.add(key)
        if current.op == "is_empty_directory" and current.value is not negated:
            return True
        child_negated = not negated if current.op == "not" else negated
        pending.extend((child, child_negated) for child in current.children)
    return False


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
    if node.op in {"file_size", "is_text", "is_image", "is_video"}:
        return frozenset({"file"})
    if node.op == "is_empty_directory":
        return frozenset({"directory"})
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
    if node.op == "file_size":
        comparison, expected = node.value
        return COMPARISONS[comparison](size, expected)
    if node.op == "is_empty_directory":
        with os.scandir(path) as entries:
            return (next(entries, None) is None) == node.value
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
