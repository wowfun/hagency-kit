from __future__ import annotations

import subprocess
import unittest
from dataclasses import replace
from unittest import mock

from typer.testing import CliRunner

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.cli import app
from hagency_cli.files.cleanup import inspection, operations, scan
from hagency_cli.files.cleanup.models import CleanRequest, ItemDisposition, PurgeRequest
from hagency_cli.files.cleanup.operations import clean_files, purge_space
from hagency_cli.files.cleanup.selection import CleanupSelectionError


class CleanupYesTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules

    def request(self, root, *, command="clean", dry_run=False):
        self.file(root / "package.json", "{}")
        self.write_rules(f'''[[rules]]
id="{command}.files"
match = 'glob="*.tmp" & file_size >= 0'
''')
        kind = CleanRequest if command == "clean" else PurgeRequest
        return kind(
            paths=(root,),
            rules=("files",),
            config=self.rules_file,
            yes=True,
            dry_run=dry_run,
        )

    def test_yes_selects_recent_candidates_without_either_prompt(self):
        for command in ("clean", "purge"):
            for interactive in (False, True):
                for dry_run in (False, True):
                    with self.subTest(
                        command=command, interactive=interactive, dry_run=dry_run
                    ):
                        root = self.base / f"{command}-{interactive}-{dry_run}"
                        target = self.file(root / "recent.tmp")
                        request = self.request(root, command=command, dry_run=dry_run)
                        ui = fixtures.CleanupUI(interactive=interactive)
                        with (
                            mock.patch.object(
                                ui,
                                "select",
                                side_effect=AssertionError("selection prompt"),
                            ),
                            mock.patch.object(
                                ui,
                                "confirm_exact",
                                side_effect=AssertionError("confirmation prompt"),
                            ),
                        ):
                            report = (
                                clean_files if command == "clean" else purge_space
                            )(request, ui=ui)
                        self.assertEqual(report.exit_code, 0)
                        self.assertEqual(report.selected_paths, (target,))
                        self.assertFalse(report.choices[0].preselected)
                        self.assertEqual(target.exists(), dry_run)
                        expected = (
                            ItemDisposition.WOULD_REMOVE
                            if dry_run
                            else ItemDisposition.REMOVED
                        )
                        self.assertEqual(report.results[0].disposition, expected)

    def test_yes_keeps_exclusion_age_and_git_protection(self):
        root = self.base / "protected"
        eligible = self.file(root / "eligible.tmp")
        excluded = self.file(root / "keep.tmp")
        young = self.file(root / "young.tmp")
        tracked = self.file(root / "tracked.tmp")
        request = self.request(root)
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "tracked.tmp"], check=True)
        with self.rules_file.open("a") as stream:
            stream.write(
                """
exclude=["keep.tmp"]
[[rules]]
id="clean.recent"
min_age_days=30
match = 'glob="young.tmp"'
"""
            )
        request = replace(request, rules=("files", "recent"))
        report = clean_files(request, ui=fixtures.CleanupUI())
        self.assertEqual(report.exit_code, 0)
        self.assertFalse(eligible.exists())
        self.assertTrue(all(path.exists() for path in (excluded, young, tracked)))

    def test_yes_revalidates_file_config_and_git_changes_after_scanning(self):
        for change in ("file", "config", "git"):
            with self.subTest(change=change):
                root = self.base / change
                target = self.file(root / "target.tmp")
                request = self.request(root)
                subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
                original = operations._build_plan

                def build(
                    value,
                    *,
                    progress,
                    target=target,
                    change=change,
                    original=original,
                    root=root,
                ):
                    plan = original(value, progress=progress)
                    if change == "file":
                        target.write_text("changed after scanning")
                    elif change == "config":
                        with self.rules_file.open("a") as stream:
                            stream.write("\n# changed after scanning\n")
                    else:
                        subprocess.run(
                            ["git", "-C", str(root), "add", "target.tmp"], check=True
                        )
                    return plan

                with mock.patch.object(operations, "_build_plan", side_effect=build):
                    report = clean_files(request, ui=fixtures.CleanupUI())
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
                self.assertTrue(target.exists())

    def test_yes_deletes_only_complete_candidates_after_timeout(self):
        root = self.base / "partial"
        self.file(root / "one.tmp")
        self.file(root / "two.tmp")
        request = self.request(root)
        original = scan._bind_candidate
        seen = []

        def bind(path, *args, **kwargs):
            seen.append(path)
            if len(seen) == 2:
                raise inspection.ScanTimeout("test timeout")
            return original(path, *args, **kwargs)

        with mock.patch.object(scan, "_bind_candidate", side_effect=bind):
            report = clean_files(request, ui=fixtures.CleanupUI())
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.selected_paths, (seen[0],))
        self.assertFalse(seen[0].exists())
        self.assertTrue(seen[1].exists())

    def test_yes_cli_aliases_dry_run_and_paths_edit_conflict(self):
        for command in ("clean", "purge"):
            for flag in ("--yes", "-y"):
                for dry_run in (False, True):
                    with self.subTest(command=command, flag=flag, dry_run=dry_run):
                        root = self.base / f"cli-{command}-{flag}-{dry_run}"
                        target = self.file(root / "target.tmp")
                        self.request(root, command=command)
                        args = [
                            "file",
                            command,
                            str(root),
                            "-r",
                            "files",
                            flag,
                            "--config",
                            str(self.rules_file),
                        ]
                        if dry_run:
                            args.append("--dry-run")
                        result = CliRunner().invoke(app, args)
                        self.assertEqual(result.exit_code, 0, result.output)
                        self.assertEqual(target.exists(), dry_run)
        result = CliRunner().invoke(app, ["file", "purge", "--paths", "-y"])
        self.assertEqual(result.exit_code, 2)

    def test_yes_request_requires_boolean(self):
        request = self.request(self.base / "invalid")
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(CleanupSelectionError):
                clean_files(replace(request, yes=value), ui=fixtures.CleanupUI())


if __name__ == "__main__":
    unittest.main()
