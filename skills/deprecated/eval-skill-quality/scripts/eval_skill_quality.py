#!/usr/bin/env python3
"""Read-only static checks for skills.

This script does not compute SRL or quality scores. SRL requires reviewer
judgment over evidence, behavior, traceability, and reproducibility.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable


PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: str
    message: str = ""
    category: str = "general"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "category": self.category,
        }


def result(name: str, status: str, message: str = "", category: str = "general") -> CheckResult:
    return CheckResult(name=name, status=status, message=message, category=category)


def read_skill(skill_path: Path) -> tuple[str | None, str, dict[str, str], str | None]:
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None, "", {}, "SKILL.md not found"

    content = skill_md.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content, content, {}, "No YAML frontmatter found"

    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return content, content, {}, "Frontmatter closing delimiter not found"

    frontmatter_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        frontmatter = parse_simple_frontmatter(frontmatter_text)
    except ValueError as exc:
        return content, body, {}, str(exc)
    return content, body, frontmatter, None


def parse_simple_frontmatter(text: str) -> dict[str, str]:
    """Parse the simple top-level YAML used by SKILL.md without external deps."""
    data: dict[str, str] = {}
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or raw.startswith((" ", "\t")):
            i += 1
            continue

        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if not match:
            raise ValueError(f"Cannot parse frontmatter line {i + 1}: {raw}")

        key, value = match.group(1), match.group(2).strip()
        if value in {"|", ">"}:
            block_lines: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line and not next_line.startswith((" ", "\t")) and re.match(
                    r"^[A-Za-z0-9_-]+:\s*", next_line
                ):
                    break
                block_lines.append(next_line.strip())
                i += 1
            separator = "\n" if value == "|" else " "
            data[key] = separator.join(line for line in block_lines if line).strip()
            continue

        data[key] = strip_yaml_scalar(value)
        i += 1

    return data


def strip_yaml_scalar(value: str) -> str:
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def check_skill_md_exists(skill_path: Path) -> CheckResult:
    if (skill_path / "SKILL.md").is_file():
        return result("SKILL.md exists", PASS, category="structure")
    return result("SKILL.md exists", FAIL, "SKILL.md is required", "structure")


def check_frontmatter(skill_path: Path) -> CheckResult:
    _, _, fm, error = read_skill(skill_path)
    if error:
        return result("SKILL.md has parseable frontmatter", FAIL, error, "structure")

    missing = [key for key in ("name", "description") if not fm.get(key)]
    if missing:
        return result(
            "SKILL.md has required frontmatter fields",
            FAIL,
            f"Missing required field(s): {', '.join(missing)}",
            "structure",
        )

    allowed = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
    unexpected = sorted(set(fm) - allowed)
    if unexpected:
        return result(
            "SKILL.md has required frontmatter fields",
            WARN,
            f"Unexpected top-level field(s): {', '.join(unexpected)}",
            "structure",
        )
    return result("SKILL.md has required frontmatter fields", PASS, category="structure")


def check_name_matches_dir(skill_path: Path) -> CheckResult:
    _, _, fm, error = read_skill(skill_path)
    if error:
        return result("Skill name matches directory", WARN, error, "structure")
    name = fm.get("name", "")
    expected = skill_path.resolve().name
    if name == expected:
        return result("Skill name matches directory", PASS, category="structure")
    return result(
        "Skill name matches directory",
        WARN,
        f"name={name!r}, directory={expected!r}",
        "structure",
    )


def check_name_shape(skill_path: Path) -> CheckResult:
    _, _, fm, error = read_skill(skill_path)
    if error:
        return result("Skill name is kebab-case", WARN, error, "structure")
    name = fm.get("name", "")
    if re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", name):
        return result("Skill name is kebab-case", PASS, category="structure")
    return result(
        "Skill name is kebab-case",
        WARN,
        "Use lowercase letters, digits, and single hyphens.",
        "structure",
    )


def check_no_extraneous_files(skill_path: Path) -> CheckResult:
    bad = {
        "README.md",
        "CHANGELOG.md",
        "INSTALLATION_GUIDE.md",
        "QUICK_REFERENCE.md",
        "LICENSE",
        "LICENSE.md",
    }
    found = [path.name for path in skill_path.iterdir() if path.name.upper() in {b.upper() for b in bad}]
    if found:
        return result(
            "No extraneous top-level files",
            WARN,
            f"Found: {', '.join(sorted(found))}",
            "structure",
        )
    return result("No extraneous top-level files", PASS, category="structure")


def check_resource_dirs_nonempty(skill_path: Path) -> CheckResult:
    empty = []
    for dirname in ("scripts", "references", "assets"):
        directory = skill_path / dirname
        if directory.is_dir():
            visible = [
                item
                for item in directory.iterdir()
                if not item.name.startswith(".") and item.name != "__pycache__"
            ]
            if not visible:
                empty.append(dirname)
    if empty:
        return result(
            "Resource directories are non-empty",
            WARN,
            f"Empty directories: {', '.join(empty)}",
            "structure",
        )
    return result("Resource directories are non-empty", PASS, category="structure")


def check_description_length(skill_path: Path) -> CheckResult:
    _, _, fm, error = read_skill(skill_path)
    if error:
        return result("Description length is useful", FAIL, error, "trigger")
    description = fm.get("description", "")
    words = len(description.split())
    chars = len(description)
    if chars > 1024:
        return result(
            "Description length is useful",
            FAIL,
            f"{chars} characters; keep under 1024.",
            "trigger",
        )
    if words < 15:
        return result(
            "Description length is useful",
            FAIL,
            f"{words} words; too short for reliable activation.",
            "trigger",
        )
    if words < 30:
        return result(
            "Description length is useful",
            WARN,
            f"{words} words; consider adding trigger contexts.",
            "trigger",
        )
    if words > 150:
        return result(
            "Description length is useful",
            WARN,
            f"{words} words; may waste always-loaded metadata context.",
            "trigger",
        )
    return result("Description length is useful", PASS, f"{words} words", "trigger")


def check_trigger_contexts(skill_path: Path) -> CheckResult:
    _, _, fm, error = read_skill(skill_path)
    if error:
        return result("Description includes trigger contexts", FAIL, error, "trigger")
    description = fm.get("description", "").lower()
    phrases = [
        "use when",
        "use for",
        "use if",
        "when asked",
        "when the user",
        "for tasks like",
        "such as",
        "especially when",
    ]
    found = [phrase for phrase in phrases if phrase in description]
    if found:
        return result(
            "Description includes trigger contexts",
            PASS,
            f"Found: {', '.join(found[:3])}",
            "trigger",
        )
    return result(
        "Description includes trigger contexts",
        WARN,
        "Add a phrase like 'Use when...' with concrete user intents.",
        "trigger",
    )


def check_body_length(skill_path: Path) -> CheckResult:
    _, body, _, error = read_skill(skill_path)
    if error:
        return result("SKILL.md body length is appropriate", FAIL, error, "documentation")
    body_lines = [line for line in body.splitlines()]
    count = len(body_lines)
    if count < 10:
        return result(
            "SKILL.md body length is appropriate",
            FAIL,
            f"{count} body lines; too short to guide behavior.",
            "documentation",
        )
    if count > 500:
        return result(
            "SKILL.md body length is appropriate",
            WARN,
            f"{count} body lines; split detailed material into references.",
            "documentation",
        )
    if count > 250:
        return result(
            "SKILL.md body length is appropriate",
            WARN,
            f"{count} body lines; review for context efficiency.",
            "documentation",
        )
    return result("SKILL.md body length is appropriate", PASS, f"{count} body lines", "documentation")


def check_references_linked(skill_path: Path) -> CheckResult:
    references = skill_path / "references"
    if not references.is_dir():
        return result("References are linked from SKILL.md", PASS, "No references directory", "documentation")

    ref_files = sorted(
        path.name
        for path in references.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )
    if not ref_files:
        return result("References are linked from SKILL.md", PASS, "No reference files", "documentation")

    skill_md = skill_path / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""
    unlinked = [name for name in ref_files if name not in body]
    if unlinked:
        return result(
            "References are linked from SKILL.md",
            WARN,
            f"Unlinked reference(s): {', '.join(unlinked)}",
            "documentation",
        )
    return result("References are linked from SKILL.md", PASS, category="documentation")


def check_ambiguity_markers(skill_path: Path) -> CheckResult:
    _content, body, fm, error = read_skill(skill_path)
    if error:
        return result("Ambiguity and conflict markers", WARN, error, "documentation")

    text = "\n".join([fm.get("description", ""), body])
    vague_phrases = [
        "when appropriate",
        "as appropriate",
        "if needed",
        "if necessary",
        "where possible",
        "as needed",
        "etc.",
        "and so on",
        "usually",
        "generally",
        "may want to",
    ]
    absolute_markers = ["always", "never", "must", "do not"]
    exception_markers = ["unless", "except", "only when"]
    precedence_markers = ["precedence", "priority", "ordered by", "default", "exception"]

    lower = text.lower()
    vague_found = [phrase for phrase in vague_phrases if phrase in lower]
    absolute_found = [phrase for phrase in absolute_markers if phrase in lower]
    exception_found = [phrase for phrase in exception_markers if phrase in lower]
    has_precedence_language = any(phrase in lower for phrase in precedence_markers)

    messages = []
    if vague_found:
        messages.append(f"Potentially vague phrase(s): {', '.join(vague_found[:8])}")
    if len(absolute_found) >= 2 and exception_found and not has_precedence_language:
        messages.append(
            "Strong rules and exceptions appear without obvious precedence language; manually check self-consistency."
        )

    if messages:
        return result(
            "Ambiguity and conflict markers",
            WARN,
            "\n".join(messages),
            "documentation",
        )
    return result(
        "Ambiguity and conflict markers",
        PASS,
        "No common ambiguity markers detected",
        "documentation",
    )


def check_python_scripts_parse(skill_path: Path) -> CheckResult:
    scripts = skill_path / "scripts"
    if not scripts.is_dir():
        return result("Python scripts parse", PASS, "No scripts directory", "scripts")

    checked = 0
    errors = []
    for script in sorted(scripts.glob("*.py")):
        checked += 1
        try:
            ast.parse(script.read_text(encoding="utf-8", errors="replace"), filename=str(script))
        except SyntaxError as exc:
            errors.append(f"{script.name}:{exc.lineno}: {exc.msg}")

    if errors:
        return result("Python scripts parse", FAIL, "\n".join(errors), "scripts")
    if checked == 0:
        return result("Python scripts parse", PASS, "No Python scripts", "scripts")
    return result("Python scripts parse", PASS, f"{checked} script(s) OK", "scripts")


def check_no_suspicious_secrets(skill_path: Path) -> CheckResult:
    patterns = [
        (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email address"),
        (
            re.compile(r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*['\"][^'\"]{8,}", re.I),
            "possible credential",
        ),
        (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style API key"),
        (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub token"),
    ]
    scanned_suffixes = {".md", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".toml"}
    findings = []

    for root, dirnames, filenames in os.walk(skill_path):
        dirnames[:] = [
            name for name in dirnames if name not in {".git", "__pycache__", "node_modules"}
        ]
        for filename in filenames:
            path = Path(root) / filename
            if path.suffix not in scanned_suffixes:
                continue
            rel = path.relative_to(skill_path)
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                for pattern, label in patterns:
                    match = pattern.search(line)
                    if not match:
                        continue
                    if label == "email address" and is_safe_example_email(match.group(0)):
                        continue
                    findings.append(f"{rel}:{line_number}: {label}")

    if findings:
        unique = list(dict.fromkeys(findings))
        shown = "\n".join(unique[:10])
        more = f"\n... {len(unique) - 10} more" if len(unique) > 10 else ""
        return result(
            "No suspicious credentials or personal emails",
            WARN,
            shown + more,
            "security",
        )
    return result("No suspicious credentials or personal emails", PASS, category="security")


def is_safe_example_email(email: str) -> bool:
    email = email.lower()
    safe_fragments = {
        "example.com",
        "example.org",
        "example.net",
        "placeholder",
        "your",
        "localhost",
    }
    return any(fragment in email for fragment in safe_fragments)


def check_env_vars_documented(skill_path: Path) -> CheckResult:
    scripts = skill_path / "scripts"
    if not scripts.is_dir():
        return result("Environment variables are documented", PASS, "No scripts directory", "security")

    env_vars: set[str] = set()
    env_pattern = re.compile(r"os\.environ(?:\.get)?\s*[\[(]\s*['\"]([A-Z][A-Z0-9_]*)['\"]")
    for script in sorted(scripts.glob("*.py")):
        text = script.read_text(encoding="utf-8", errors="replace")
        env_vars.update(env_pattern.findall(text))

    if not env_vars:
        return result("Environment variables are documented", PASS, "No env vars found", "security")

    skill_text = (skill_path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    undocumented = sorted(var for var in env_vars if var not in skill_text)
    if undocumented:
        return result(
            "Environment variables are documented",
            WARN,
            f"Undocumented env var(s): {', '.join(undocumented)}",
            "security",
        )
    return result(
        "Environment variables are documented",
        PASS,
        f"All {len(env_vars)} env var(s) documented",
        "security",
    )


CHECKS: list[Callable[[Path], CheckResult]] = [
    check_skill_md_exists,
    check_frontmatter,
    check_name_matches_dir,
    check_name_shape,
    check_no_extraneous_files,
    check_resource_dirs_nonempty,
    check_description_length,
    check_trigger_contexts,
    check_body_length,
    check_references_linked,
    check_ambiguity_markers,
    check_python_scripts_parse,
    check_no_suspicious_secrets,
    check_env_vars_documented,
]


def run_checks(skill_path: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.append(check(skill_path))
        except Exception as exc:  # pragma: no cover - defensive CLI boundary
            name = getattr(check, "__name__", "unknown check")
            results.append(result(name, FAIL, f"Check crashed: {exc}", "internal"))
    return results


def summarize(results: list[CheckResult]) -> dict[str, int | float]:
    counts = {
        PASS: sum(1 for item in results if item.status == PASS),
        WARN: sum(1 for item in results if item.status == WARN),
        FAIL: sum(1 for item in results if item.status == FAIL),
    }
    total = len(results)
    counts["total"] = total
    counts["structural_score"] = round((counts[PASS] / total) * 100, 1) if total else 0.0
    return counts


def print_human_report(skill_path: Path, results: list[CheckResult], verbose: bool) -> None:
    summary = summarize(results)
    print(f"Skill quality static check: {skill_path.resolve().name}")
    print(f"Path: {skill_path.resolve()}")
    print()

    by_category: dict[str, list[CheckResult]] = {}
    for item in results:
        by_category.setdefault(item.category, []).append(item)

    for category in ("structure", "trigger", "documentation", "scripts", "security", "internal"):
        items = by_category.get(category)
        if not items:
            continue
        print(f"[{category}]")
        for item in items:
            print(f"  {item.status.upper():4} {item.name}")
            if item.message and (verbose or item.status != PASS):
                for line in item.message.splitlines():
                    print(f"       {line}")
        print()

    print(
        f"Summary: {summary[PASS]} pass, {summary[WARN]} warn, {summary[FAIL]} fail "
        f"({summary['structural_score']}% pass rate)"
    )
    print(
        "Note: this is a static check only. Use references/rubric.md and "
        "references/srl-framework.md for scored quality and SRL review."
    )


def print_json_report(skill_path: Path, results: list[CheckResult]) -> None:
    payload = {
        "skill": skill_path.resolve().name,
        "path": str(skill_path.resolve()),
        "checks": [item.to_dict() for item in results],
        "summary": summarize(results),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run read-only static checks on a skill directory. "
            "Does not compute SRL or quality scores."
        )
    )
    parser.add_argument("skill_path", help="Path to the skill directory")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show messages for passing checks")
    args = parser.parse_args(argv)

    skill_path = Path(args.skill_path)
    if not skill_path.is_dir():
        message = f"Error: {skill_path} is not a directory"
        if args.json:
            print(json.dumps({"error": message}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 2

    results = run_checks(skill_path)
    if args.json:
        print_json_report(skill_path, results)
    else:
        print_human_report(skill_path, results, args.verbose)

    if any(item.name == "SKILL.md exists" and item.status == FAIL for item in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
