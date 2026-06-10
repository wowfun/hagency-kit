#!/usr/bin/env python3
"""Build CSV/XLSX evidence tables for git diff analysis."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


MAIN_COLUMNS = [
    "file_path",
    "file_extension",
    "has_add",
    "has_sub",
    "changed_lines",
]

SPECIAL_COLUMNS = [
    "file_path",
    "file_extension",
    "change_type",
    "old_path",
    "is_binary",
    "reason",
    "raw_additions",
    "raw_deletions",
    "notes",
]


def run_git(repo: Path, args: Sequence[str], *, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    return result.stdout


def repo_root(repo: Path) -> Path:
    output = run_git(repo, ["rev-parse", "--show-toplevel"])
    return Path(str(output).strip())


def parse_numstat_z(raw: bytes) -> List[Dict[str, Optional[str]]]:
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens = tokens[:-1]

    rows: List[Dict[str, Optional[str]]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i].decode("utf-8", errors="replace")
        parts = token.split("\t")
        if len(parts) < 3:
            i += 1
            continue

        additions, deletions, path = parts[0], parts[1], parts[2]
        if path == "" and i + 2 < len(tokens):
            old_path = tokens[i + 1].decode("utf-8", errors="replace")
            new_path = tokens[i + 2].decode("utf-8", errors="replace")
            rows.append(
                {
                    "path": new_path,
                    "old_path": old_path,
                    "additions": additions,
                    "deletions": deletions,
                }
            )
            i += 3
            continue

        rows.append(
            {
                "path": path,
                "old_path": None,
                "additions": additions,
                "deletions": deletions,
            }
        )
        i += 1

    return rows


def parse_name_status_z(raw: bytes) -> Dict[str, Dict[str, Optional[str]]]:
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens = tokens[:-1]

    info: Dict[str, Dict[str, Optional[str]]] = {}
    i = 0
    while i < len(tokens):
        status = tokens[i].decode("utf-8", errors="replace")
        code = status[0] if status else ""
        if code in {"R", "C"} and i + 2 < len(tokens):
            old_path = tokens[i + 1].decode("utf-8", errors="replace")
            new_path = tokens[i + 2].decode("utf-8", errors="replace")
            info[new_path] = {
                "change_type": code,
                "old_path": old_path,
                "score": status[1:] or None,
            }
            i += 3
            continue

        if i + 1 < len(tokens):
            path = tokens[i + 1].decode("utf-8", errors="replace")
            info[path] = {
                "change_type": code,
                "old_path": None,
                "score": status[1:] or None,
            }
        i += 2

    return info


def is_binary_numstat(additions: str, deletions: str) -> bool:
    return additions == "-" or deletions == "-"


def to_int(value: str) -> int:
    return int(value) if value.isdigit() else 0


def extension_for(path: str) -> str:
    suffix = Path(path).suffix
    return suffix[1:] if suffix.startswith(".") else suffix


def build_tracked_tables(
    numstat_rows: Iterable[Dict[str, Optional[str]]],
    status_info: Dict[str, Dict[str, Optional[str]]],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    main_rows: List[Dict[str, object]] = []
    special_rows: List[Dict[str, object]] = []

    for row in numstat_rows:
        path = str(row["path"] or "")
        additions = str(row["additions"] or "0")
        deletions = str(row["deletions"] or "0")
        status = status_info.get(path, {})
        change_type = str(status.get("change_type") or "")
        old_path = status.get("old_path") or row.get("old_path") or ""
        is_binary = is_binary_numstat(additions, deletions)
        is_special = is_binary or change_type in {"R", "C"}

        if is_special:
            reasons = []
            if is_binary:
                reasons.append("binary")
            if change_type == "R":
                reasons.append("rename")
            if change_type == "C":
                reasons.append("copy")
            special_rows.append(
                {
                    "file_path": path,
                    "file_extension": extension_for(path),
                    "change_type": change_type,
                    "old_path": old_path,
                    "is_binary": is_binary,
                    "reason": "+".join(reasons),
                    "raw_additions": additions,
                    "raw_deletions": deletions,
                    "notes": "",
                }
            )
            continue

        add_count = to_int(additions)
        del_count = to_int(deletions)
        main_rows.append(
            {
                "file_path": path,
                "file_extension": extension_for(path),
                "has_add": add_count > 0,
                "has_sub": del_count > 0,
                "changed_lines": add_count + del_count,
            }
        )

    return (
        pd.DataFrame(main_rows, columns=MAIN_COLUMNS),
        pd.DataFrame(special_rows, columns=SPECIAL_COLUMNS),
    )


def looks_binary(path: Path, sample_size: int = 8192) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(sample_size)
    except OSError:
        return False


def count_text_lines(path: Path) -> Optional[int]:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def append_untracked(repo: Path, special_df: pd.DataFrame) -> pd.DataFrame:
    raw = run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"], binary=True)
    paths = [p.decode("utf-8", errors="replace") for p in bytes(raw).split(b"\0") if p]
    rows: List[Dict[str, object]] = []

    for rel_path in paths:
        abs_path = repo / rel_path
        is_binary = looks_binary(abs_path)
        line_count = None if is_binary else count_text_lines(abs_path)
        rows.append(
            {
                "file_path": rel_path,
                "file_extension": extension_for(rel_path),
                "change_type": "U",
                "old_path": "",
                "is_binary": is_binary,
                "reason": "untracked",
                "raw_additions": "" if line_count is None else line_count,
                "raw_deletions": 0 if line_count is not None else "",
                "notes": "untracked file from worktree mode",
            }
        )

    if not rows:
        return special_df
    return pd.concat([special_df, pd.DataFrame(rows, columns=SPECIAL_COLUMNS)], ignore_index=True)


def resolve_range(repo: Path, args: argparse.Namespace) -> Tuple[List[str], str, bool]:
    commit_mode = args.base is not None or args.target is not None
    branch_mode = args.base_branch is not None or args.target_branch is not None

    if commit_mode and branch_mode:
        raise SystemExit("Use either commit range or branch compare, not both.")
    if commit_mode and not (args.base and args.target):
        raise SystemExit("Commit range requires both --base and --target.")
    if branch_mode and not (args.base_branch and args.target_branch):
        raise SystemExit("Branch compare requires both --base-branch and --target-branch.")

    if commit_mode:
        diff_range = f"{args.base}..{args.target}"
        return [diff_range], diff_range, False

    if branch_mode:
        merge_base = str(run_git(repo, ["merge-base", args.base_branch, args.target_branch])).strip()
        diff_range = f"{merge_base}..{args.target_branch}"
        label = f"{args.base_branch}...{args.target_branch} ({diff_range})"
        return [diff_range], label, False

    return ["HEAD"], "HEAD..worktree", True


def write_outputs(
    output_dir: Path,
    output_format: str,
    main_df: pd.DataFrame,
    special_df: pd.DataFrame,
) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    if output_format == "csv":
        main_path = output_dir / "file-change-table.csv"
        special_path = output_dir / "special-files.csv"
        main_df.to_csv(main_path, index=False)
        special_df.to_csv(special_path, index=False)
        written.extend([main_path, special_path])
        return written

    workbook = output_dir / "file-change-table.xlsx"
    with pd.ExcelWriter(workbook) as writer:
        main_df.to_excel(writer, sheet_name="file_changes", index=False)
        special_df.to_excel(writer, sheet_name="special_files", index=False)
    written.append(workbook)
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate file change tables for analyze-diff.",
    )
    parser.add_argument("--repo", default=".", help="Git repository path. Defaults to current directory.")
    parser.add_argument("--base", help="Base commit for direct base..target comparison.")
    parser.add_argument("--target", help="Target commit for direct base..target comparison.")
    parser.add_argument("--base-branch", help="Base branch for merge-base branch comparison.")
    parser.add_argument("--target-branch", help="Target branch for merge-base branch comparison.")
    parser.add_argument("--output-dir", default=".local/diff", help="Output directory, relative to repo root unless absolute.")
    parser.add_argument("--format", choices=["csv", "xlsx"], default="csv", help="Output format. Defaults to csv.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = repo_root(Path(args.repo).resolve())
    diff_args, range_label, include_untracked = resolve_range(root, args)

    numstat_raw = run_git(root, ["diff", "--numstat", "-M", "-z", *diff_args], binary=True)
    name_status_raw = run_git(root, ["diff", "--name-status", "-M", "-z", *diff_args], binary=True)

    main_df, special_df = build_tracked_tables(
        parse_numstat_z(bytes(numstat_raw)),
        parse_name_status_z(bytes(name_status_raw)),
    )
    if include_untracked:
        special_df = append_untracked(root, special_df)

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    written = write_outputs(output_dir, args.format, main_df, special_df)

    print(f"range: {range_label}")
    print(f"repo: {root}")
    print(f"main_rows: {len(main_df)}")
    print(f"special_rows: {len(special_df)}")
    for path in written:
        print(f"wrote: {path}")


if __name__ == "__main__":
    main()
