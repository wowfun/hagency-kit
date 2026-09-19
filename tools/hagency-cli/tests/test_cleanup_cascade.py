from __future__ import annotations

import unittest
from unittest import mock

from tests import test_cleanup as fixtures
from tests import test_cleanup_project_default as project_fixtures

# isort: split

from hagency_cli.files.cleanup import inspection, operations, scan
from hagency_cli.files.cleanup.models import CleanRequest, ItemDisposition


class CascadeTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules
    repository_policy = project_fixtures.ProjectDefaultTests.repository_policy
    run_rule = project_fixtures.ProjectDefaultTests.run_rule

    def test_sibling_branches_empty_their_shared_ancestors_once(self):
        self.repository_policy()
        leaves = (
            self.file(self.repo / "a/b/deep/first.log"),
            self.file(self.repo / "a/c/second.log"),
            self.file(self.repo / "a/third.log"),
        )
        report = self.run_rule(yes=True)
        self.assertEqual(report.exit_code, 0, report.issues)
        expected = {
            *leaves,
            *(self.repo / name for name in ("a/b/deep", "a/b", "a/c", "a")),
        }
        self.assertEqual(set(report.selected_paths), expected)
        self.assertEqual(len(report.selected_paths), len(expected))
        self.assertFalse((self.repo / "a").exists())
        self.assertTrue(self.repo.exists())

    def test_failed_child_does_not_delete_shared_parent_or_retry_failure(self):
        self.repository_policy()
        first = self.file(self.repo / "a/b/first.log")
        second = self.file(self.repo / "a/c/second.log")
        remove = operations._permanently_remove
        attempted = []

        def fail_directory(candidate):
            path = candidate.choice.exact_path
            attempted.append(path)
            if path == first.parent:
                raise OSError("directory removal failed")
            remove(candidate)

        with mock.patch.object(operations, "_permanently_remove", fail_directory):
            report = self.run_rule(yes=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(first.parent.is_dir())
        self.assertFalse(second.parent.exists())
        self.assertTrue(first.parent.parent.is_dir())
        self.assertEqual(attempted.count(first.parent), 1)
        self.assertNotIn(first.parent.parent, attempted)

    def test_binding_and_revalidation_timeouts_are_reported_after_file_removal(self):
        for stage in ("_bind_candidate", "_revalidate"):
            with self.subTest(stage=stage):
                self.repository_policy()
                leaf = self.file(self.repo / stage / "drop.log")
                original = getattr(operations, stage)

                def timeout(*args, stage=stage, leaf=leaf, original=original, **kwargs):
                    path = (
                        args[0]
                        if stage == "_bind_candidate"
                        else args[0].choice.exact_path
                    )
                    if path == leaf.parent:
                        raise inspection.ScanTimeout("injected cascade timeout")
                    return original(*args, **kwargs)

                with mock.patch.object(operations, stage, timeout):
                    report = self.run_rule(yes=True)
                self.assertEqual(report.exit_code, 1)
                self.assertFalse(leaf.exists())
                self.assertTrue(leaf.parent.is_dir())
                messages = [issue.message for issue in report.issues] + [
                    result.message for result in report.results
                ]
                self.assertTrue(
                    any("injected cascade timeout" in message for message in messages)
                )

    def test_binding_runs_inside_a_budget_with_a_fresh_mount_snapshot(self):
        self.repository_policy()
        self.file(self.repo / "parent/drop.log")
        bind = operations._bind_candidate
        seen = []

        def binding(*args, **kwargs):
            seen.append(args[0])
            self.assertIsNotNone(inspection._DEADLINE.get())
            self.assertIsNotNone(inspection._MOUNTS.get())
            return bind(*args, **kwargs)

        with mock.patch.object(operations, "_bind_candidate", binding):
            report = self.run_rule(yes=True)
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertEqual(seen, [self.repo / "parent"])

    def test_cascade_never_uses_an_alternate_branch_to_remove_nonempty_directories(
        self,
    ):
        self.write_rules("""[[rules]]
id="clean.custom"
match='is_empty_directory | glob="parent/" | is_text'
""")
        leaf = self.file(self.repo / "parent/drop.txt")
        kept = self.file(self.repo / "parent/keep.bin")
        plan = scan._build_plan(
            CleanRequest(config=self.rules_file, paths=(self.repo,), rules=("custom",))
        )
        leaf.unlink()
        selected, results, issues = operations._cascade_empty_directories(
            plan, (leaf,), set()
        )
        self.assertTrue(kept.exists())
        self.assertEqual((selected, results, issues), ((), (), ()))

    def test_content_arriving_during_cascade_binding_is_never_authorized(self):
        self.write_rules("""[[rules]]
id="clean.custom"
match='is_empty_directory | glob="parent/" | is_text'
""")
        leaf = self.file(self.repo / "parent/drop.txt")
        plan = scan._build_plan(
            CleanRequest(config=self.rules_file, paths=(self.repo,), rules=("custom",))
        )
        leaf.unlink()
        bind = operations._bind_candidate
        newcomer = leaf.with_name("arrived.bin")

        def arrival(*args, **kwargs):
            self.file(newcomer)
            return bind(*args, **kwargs)

        with mock.patch.object(operations, "_bind_candidate", arrival):
            selected, results, _issues = operations._cascade_empty_directories(
                plan, (leaf,), set()
            )
        self.assertTrue(newcomer.exists())
        self.assertEqual(selected, ())
        self.assertEqual(results[0].disposition, ItemDisposition.SKIPPED)

    def test_each_ancestor_refreshes_mount_checks_after_previous_removal(self):
        self.repository_policy()
        leaf = self.file(self.repo / "a/b/drop.log")
        mounted = set()
        remove = operations._permanently_remove

        def mount_parent(candidate):
            remove(candidate)
            if candidate.choice.exact_path == leaf.parent:
                mounted.add(leaf.parent.parent)

        with (
            mock.patch.object(
                inspection, "_mount_points", side_effect=lambda: frozenset(mounted)
            ),
            mock.patch.object(operations, "_permanently_remove", mount_parent),
        ):
            report = self.run_rule(yes=True)
        self.assertFalse(leaf.parent.exists())
        self.assertTrue(leaf.parent.parent.is_dir())
        self.assertFalse(
            any(
                result.exact_path == leaf.parent.parent
                and result.disposition is ItemDisposition.REMOVED
                for result in report.results
            )
        )

    def test_project_sources_match_empty_directories_without_enabling_cascade(self):
        self.write_rules("""[[rules]]
id="purge.files"
match='source="project" & (is_empty_directory | (glob="*.log" & is_text))'
[[rules]]
id="clean.project"
match='ref="purge.files"'
""")
        self.file(self.repo / "package.json", "{}")
        leaf = self.file(self.repo / "parent/drop.log")
        empty = self.repo / "existing-empty"
        empty.mkdir()
        report = self.run_rule(rules=("project",), yes=True)
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertFalse(leaf.exists())
        self.assertFalse(empty.exists())
        self.assertTrue(leaf.parent.is_dir())

    def test_overlapping_anchors_keep_each_glob_base(self):
        self.write_rules("""[[rules]]
id="clean.nested"
match='(within="a" & (glob="/b/*.txt" | (glob="/b/" & is_empty_directory))) | (within="a/b" & (glob="/*.txt" | is_empty_directory))'
""")
        leaf = self.file(self.repo / "a/b/drop.txt")
        report = self.run_rule(rules=("nested",), paths=(), yes=True)
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertEqual(set(report.selected_paths), {leaf, leaf.parent})
        self.assertFalse(leaf.parent.exists())
        self.assertTrue(leaf.parent.parent.is_dir())
