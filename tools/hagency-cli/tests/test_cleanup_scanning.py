from __future__ import annotations

import io
import os
import subprocess
import unittest
from dataclasses import replace
from unittest import mock

from typer.testing import CliRunner

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.cli import app
from hagency_cli.commands.cleanup_ui import QuestionaryCleanupUI
from hagency_cli.files.cleanup import inspection, planning, scan
from hagency_cli.files.cleanup.models import (
    CleanRequest,
    ItemDisposition,
    PurgeRequest,
    ScanProgress,
)
from hagency_cli.files.cleanup.operations import clean_files, purge_space
from hagency_cli.files.cleanup.selection import CleanupSelectionError


class LargeScanTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules

    def request(self, *roots, command="clean", timeout=0):
        self.write_rules(f'''[[rules]]
id="{command}.files"
match = {{ all = [{{ glob = "*.bin" }}, {{ min_file_size = 0 }}] }}
''')
        kind = CleanRequest if command == "clean" else PurgeRequest
        return kind(
            paths=tuple(roots),
            rules=("files",),
            config=self.rules_file,
            scan_timeout=timeout,
        )

    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )

    def test_timeouts_at_git_measurement_and_scan_entry_keep_only_complete_candidates(
        self,
    ):
        for stage in ("entry", "git", "measure"):
            for command in ("clean", "purge"):
                with self.subTest(stage=stage, command=command):
                    root = self.base / f"{command}-{stage}"
                    first = self.file(root / "first.bin")
                    second = self.file(root / "second.bin")
                    self.file(root / "package.json", "{}")
                    request = self.request(root, command=command, timeout=60)
                    function = clean_files if command == "clean" else purge_space
                    seen = []
                    original = scan._bind_candidate

                    def bind(
                        path, *args, seen=seen, stage=stage, original=original, **kwargs
                    ):
                        seen.append(path)
                        if len(seen) == 1 and stage != "entry":
                            return original(path, *args, **kwargs)
                        if stage == "entry":
                            raise inspection.ScanTimeout("entry timeout")
                        if stage == "git":
                            with mock.patch.object(
                                planning.PlanningSession,
                                "git_contexts",
                                side_effect=inspection.ScanTimeout("git timeout"),
                            ):
                                return original(path, *args, **kwargs)
                        with mock.patch.object(
                            scan,
                            "_measure_candidate",
                            side_effect=inspection.ScanTimeout("measure timeout"),
                        ):
                            return original(path, *args, **kwargs)

                    with mock.patch.object(scan, "_bind_candidate", side_effect=bind):
                        report = function(
                            request, ui=fixtures.CleanupUI(interactive=True)
                        )
                    self.assertEqual(report.exit_code, 1)
                    self.assertTrue(
                        any(issue.code == "scan_timeout" for issue in report.issues)
                    )
                    if stage == "entry":
                        self.assertEqual(report.choices, ())
                        self.assertTrue(first.exists() and second.exists())
                    else:
                        self.assertEqual(
                            [item.exact_path for item in report.choices], seen[:1]
                        )
                        self.assertEqual(
                            report.results[0].disposition, ItemDisposition.REMOVED
                        )
                        self.assertFalse(seen[0].exists())
                        self.assertTrue(seen[1].exists())

    def test_shared_budget_is_not_reset_between_roots(self):
        for command in ("clean", "purge"):
            first = self.file(self.base / command / "one/a.bin")
            second = self.file(self.base / command / "two/b.bin")
            for root in (first.parent, second.parent):
                self.file(root / "package.json", "{}")
            request = self.request(
                first.parent, second.parent, command=command, timeout=5
            )
            original = scan._scan_root
            readings = []
            clock = [100.0]

            def scan_root(
                *args, readings=readings, original=original, clock=clock, **kwargs
            ):
                readings.append(inspection._DEADLINE.get() - planning.time.monotonic())
                result = original(*args, **kwargs)
                clock[0] += 3
                return result

            with (
                mock.patch.object(
                    planning.time, "monotonic", side_effect=lambda clock=clock: clock[0]
                ),
                mock.patch.object(scan, "_scan_root", side_effect=scan_root),
            ):
                report = (clean_files if command == "clean" else purge_space)(
                    request, ui=fixtures.CleanupUI()
                )
            self.assertEqual(readings, [5, 2])
            self.assertEqual(
                report.exit_code, 1
            )  # Finalizing also consumes the shared budget.
            # Purge candidates still need cross-root protection merging; never
            # publish them when that phase has not completed.
            self.assertEqual(len(report.choices), 2 if command == "clean" else 0)

    def test_default_scan_has_no_deadline_and_finalization_timeout_keeps_complete_targets(
        self,
    ):
        target = self.file(self.base / "cache/a.bin")
        request = self.request(target.parent)
        original = scan._scan_root

        def root(*args, **kwargs):
            self.assertIsNone(inspection._DEADLINE.get())
            return original(*args, **kwargs)

        with mock.patch.object(scan, "_scan_root", side_effect=root):
            self.assertEqual(clean_files(request, ui=fixtures.CleanupUI()).exit_code, 0)
        deduplicate = scan._drop_duplicate_and_nested
        calls = []

        def finish(candidates):
            calls.append(True)
            if len(calls) == 1:
                raise inspection.ScanTimeout("finalizing timeout")
            return deduplicate(candidates)

        with mock.patch.object(scan, "_drop_duplicate_and_nested", side_effect=finish):
            report = clean_files(request, ui=fixtures.CleanupUI(interactive=True))
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.REMOVED)

    def test_invalid_budgets_cli_edit_conflicts_and_interrupt(self):
        target = self.file(self.base / "cache/a.bin")
        request = self.request(target.parent)
        for timeout in (-1, True, 0.5, "60"):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(CleanupSelectionError),
            ):
                clean_files(
                    replace(request, scan_timeout=timeout), ui=fixtures.CleanupUI()
                )
        for command in ("clean", "purge"):
            for timeout in ("-1", "1.5"):
                result = CliRunner().invoke(
                    app, ["file", command, "--scan-timeout", timeout]
                )
                self.assertEqual(result.exit_code, 2)
        result = CliRunner().invoke(
            app, ["file", "purge", "--paths", "--scan-timeout", "0"]
        )
        self.assertEqual(result.exit_code, 2)
        events = []
        ui = fixtures.CleanupUI(interactive=True)
        ui.scan_progress = events.append
        with mock.patch.object(scan, "_scan_root", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                clean_files(request, ui=ui)
            result = CliRunner().invoke(
                app,
                [
                    "file",
                    "clean",
                    str(target.parent),
                    "-r",
                    "files",
                    "--config",
                    str(self.rules_file),
                ],
            )
        self.assertEqual(result.exit_code, 130)
        self.assertTrue(events[-1].finished)
        self.assertTrue(target.exists())

    def test_git_cache_handles_nested_repositories_and_rechecks_staging_before_delete(
        self,
    ):
        root = self.base / "git"
        tracked = self.file(root / "tracked.bin")
        untracked = self.file(root / "fresh.bin")
        nested = self.file(root / "nested/inner.bin")
        self.git(root, "init", "-q")
        self.git(root, "add", "tracked.bin")
        self.git(nested.parent, "init", "-q")
        self.git(nested.parent, "add", "inner.bin")
        request = self.request(root)
        events = []
        ui = fixtures.CleanupUI(
            interactive=True, hook=lambda: self.git(root, "add", "fresh.bin")
        )
        ui.scan_progress = events.append
        report = clean_files(request, ui=ui)
        self.assertEqual([item.exact_path for item in report.choices], [untracked])
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue(all(path.exists() for path in (tracked, untracked, nested)))
        self.assertEqual(events[-1].git_queries, 2)
        # A later invocation gets a new cache.
        self.assertEqual(clean_files(request, ui=fixtures.CleanupUI()).choices, ())

    def test_git_cache_preserves_literal_names_and_worktree_index(self):
        root = self.base / "repo-files"
        special = self.file(root / "[x]*.bin")
        other = self.file(root / "x-file.bin")
        newline = self.file(root / "line\nbreak.bin")
        self.git(root, "init", "-q")
        self.git(root, "add", "--all")
        self.git(
            root,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        )
        worktree = self.base / "worktree"
        self.git(root, "worktree", "add", "--detach", str(worktree))
        with planning.planning_session(0, None) as session:
            for repository in (root, worktree):
                for name in (special.name, other.name, newline.name):
                    self.assertEqual(
                        session.git_tracked(repository / name, repository), (True, None)
                    )
                self.assertEqual(
                    session.git_tracked(repository / "unknown.bin", repository),
                    (False, None),
                )
            self.assertEqual(session.git_queries, 2)

    def test_git_failure_is_cached_and_never_allows_candidates(self):
        target = self.file(self.base / "git/a.bin")
        self.file(target.parent / "b.bin")
        self.git(target.parent, "init", "-q")
        request = self.request(target.parent)
        with mock.patch.object(
            planning.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, b"", b"index failure"),
        ) as run:
            report = clean_files(request, ui=fixtures.CleanupUI())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report.choices, ())
        self.assertEqual(report.exit_code, 1)

    def test_progress_is_throttled_and_tty_only(self):
        events = []
        clock = [100.0]
        with (
            mock.patch.object(planning.time, "monotonic", side_effect=lambda: clock[0]),
            planning.planning_session(0, events.append) as session,
        ):
            for _ in range(100):
                session.update("scanning", self.base)
            self.assertEqual(len(events), 1)
            clock[0] += 0.21
            session.tick()
            self.assertEqual(len(events), 2)
        self.assertTrue(events[-1].finished)
        for tty in (False, True):
            stream = io.StringIO()
            with (
                mock.patch("sys.stderr", stream),
                mock.patch.object(stream, "isatty", return_value=tty),
            ):
                ui = QuestionaryCleanupUI()
                ui.scan_progress(ScanProgress("scanning", self.base, 10, 2, 1))
                ui.scan_progress(
                    ScanProgress("scanning", self.base, 10, 2, 1, finished=True)
                )
            self.assertEqual(bool(stream.getvalue()), tty)

    def test_progress_counts_file_only_and_whole_directory_scans_once(self):
        root = self.base / "cache"
        self.file(root / "first.bin")
        self.file(root / "folder/second.bin")
        for condition in ("min_file_size=0", ""):
            with self.subTest(condition=condition):
                self.write_rules(
                    """[[rules]]
id="clean.files"
match = {"""
                    + (condition or 'glob="**/*"')
                    + "}"
                )
                events = []
                ui = fixtures.CleanupUI()
                ui.scan_progress = events.append
                report = clean_files(
                    CleanRequest(
                        paths=(root,), rules=("files",), config=self.rules_file
                    ),
                    ui=ui,
                )
                self.assertEqual(report.exit_code, 0)
                self.assertEqual(events[-1].entries, 3)

    def test_progress_uses_stderr_terminal_width_when_stdout_is_redirected(self):
        stream = io.StringIO()
        with (
            mock.patch("sys.stderr", stream),
            mock.patch.object(stream, "isatty", return_value=True),
            mock.patch.object(stream, "fileno", return_value=17),
            mock.patch(
                "os.get_terminal_size", return_value=os.terminal_size((40, 24))
            ) as size,
        ):
            QuestionaryCleanupUI().scan_progress(
                ScanProgress("scanning", self.base, 10, 2, 1)
            )
        size.assert_called_once_with(17)
        self.assertEqual(len(stream.getvalue().removeprefix("\r\033[2K")), 39)

    def test_agent_budget_remains_independent_and_cancelled_partial_scan_fails(self):
        root = self.base / "staging"
        root.mkdir()
        budgets = []

        def inventory():
            budgets.append(inspection._BUDGET_SECONDS.get())
            return []

        with (
            mock.patch.object(scan.agents, "source_roots", return_value=(root,)),
            mock.patch.object(scan.agents, "staging_inventory", side_effect=inventory),
        ):
            report = clean_files(
                CleanRequest(
                    config=self.rules_file, rules=("codex.staging",), scan_timeout=1
                ),
                ui=fixtures.CleanupUI(),
            )
        self.assertEqual(budgets, [60])
        self.assertEqual(report.exit_code, 0)

        target = self.file(self.base / "cache/a.bin")
        request = self.request(target.parent)
        deduplicate = scan._drop_duplicate_and_nested
        calls = []

        def timeout_once(candidates):
            calls.append(True)
            if len(calls) == 1:
                raise inspection.ScanTimeout("timeout")
            return deduplicate(candidates)

        ui = fixtures.CleanupUI(interactive=True)
        with (
            mock.patch.object(
                scan, "_drop_duplicate_and_nested", side_effect=timeout_once
            ),
            mock.patch.object(ui, "select", return_value=()),
        ):
            report = clean_files(request, ui=ui)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(target.exists())

    def test_sibling_files_reuse_ancestor_discovery_and_git_timeout_propagates(self):
        target = self.file(self.base / "cache/a.bin")
        sibling = self.file(target.parent / "b.bin")
        self.git(target.parent, "init", "-q")
        with planning.planning_session(0, None) as session:
            with mock.patch.object(
                inspection,
                "_git_context_from_marker",
                wraps=inspection._git_context_from_marker,
            ) as marker:
                first, error = session.git_contexts(target)
                calls = marker.call_count
                self.assertIsNone(error)
                self.assertEqual(session.git_contexts(sibling), (first, None))
                self.assertEqual(marker.call_count, calls)
            with (
                mock.patch.object(
                    planning.subprocess,
                    "run",
                    side_effect=inspection.ScanTimeout("git deadline"),
                ),
                self.assertRaises(inspection.ScanTimeout),
            ):
                session.git_tracked(target, target.parent)
            self.assertNotIn(target.parent, session.tracked)

    def test_hundred_thousand_candidates_use_one_git_query_and_ancestor_index(self):
        target = self.file(self.base / "large/seed.bin")
        request = self.request(target.parent)
        candidate = scan._build_plan(request).candidates[0]
        paths = [
            target.parent / f"group-{index // 1000}" / f"{index}.bin"
            for index in range(100_000)
        ]
        candidates = [
            replace(candidate, choice=replace(candidate.choice, exact_path=path))
            for path in paths
        ]
        with mock.patch.object(scan, "_canonical_key", side_effect=str) as canonical:
            with mock.patch.object(
                scan, "_is_within", side_effect=AssertionError("quadratic containment")
            ):
                self.assertEqual(
                    len(scan._drop_duplicate_and_nested(candidates)), 100_000
                )
            self.assertEqual(canonical.call_count, 100_000)
        output = (
            b"\0".join(os.fsencode(path.relative_to(target.parent)) for path in paths)
            + b"\0"
        )
        with (
            planning.planning_session(0, None) as session,
            mock.patch.object(
                planning.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, output, b""),
            ) as run,
        ):
            for path in paths:
                self.assertEqual(session.git_tracked(path, target.parent), (True, None))
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
