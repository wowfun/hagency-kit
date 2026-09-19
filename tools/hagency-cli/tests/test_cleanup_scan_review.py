from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests import test_cleanup_project_default as fixtures

# isort: split

from hagency_cli.files.cleanup import matching, scan
from hagency_cli.files.cleanup.conditions import Condition
from hagency_cli.files.cleanup.models import RuleScope, ScanRule


class ScanReviewTests(unittest.TestCase):
    setUp = fixtures.ProjectDefaultTests.setUp
    file = fixtures.ProjectDefaultTests.file
    write_rules = fixtures.ProjectDefaultTests.write_rules
    repository_policy = fixtures.ProjectDefaultTests.repository_policy
    run_rule = fixtures.ProjectDefaultTests.run_rule

    def test_paths_do_not_enter_completed_purge_directories(self):
        self.repository_policy()
        artifact = self.file(self.repo / ".local/node_modules/a/b/payload.bin").parents[
            2
        ]
        remaining = self.file(self.repo / ".local/standalone.bin")
        original = scan._scan_root
        visits = []
        reads = []
        scandir = scan.os.scandir

        class TrackedScan:
            def __init__(self, path):
                self.stream = scandir(path)
                self.counter = [path, 0]
                reads.append(self.counter)

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self.stream)
                self.counter[1] += 1
                return entry

        def record(path):
            visits.append(path)
            return TrackedScan(path)

        def scanning(*args, **kwargs):
            if kwargs.get("path_scopes") is not None:
                with mock.patch.object(scan.os, "scandir", record):
                    return original(*args, **kwargs)
            return original(*args, **kwargs)

        with mock.patch.object(scan, "_scan_root", scanning):
            report = self.run_rule()
        self.assertEqual({c.exact_path for c in report.choices}, {artifact, remaining})
        self.assertNotIn(artifact, visits)
        self.assertFalse(any(path.is_relative_to(artifact) for path in visits))
        # One traversal reads both children; the other two opens are single-entry
        # empty-directory probes, not additional traversals of this subtree.
        self.assertEqual(
            sorted(count for path, count in reads if path == self.repo / ".local"),
            [1, 1, 2],
        )

    def test_protected_purge_directory_still_receives_path_scan(self):
        self.repository_policy()
        self.rules_file.write_text(
            self.rules_file.read_text().replace(
                'id = "purge.artifacts"',
                'id = "purge.artifacts"\nexclude = ["**/keep.txt"]',
                1,
            )
        )
        keep = self.file(self.repo / ".local/node_modules/keep.txt")
        target = self.file(keep.parent / "drop.bin")
        report = self.run_rule(yes=True)
        self.assertEqual(report.exit_code, 0, report.results)
        self.assertTrue(keep.exists())
        self.assertFalse(target.exists())

    def test_recursive_rule_does_not_walk_unrelated_deep_roots(self):
        self.write_rules("""[[rules]]
id="clean.deep"
recursive=true
include_unverified=true
match='within="recursive" & ref="purge.artifacts"'
[[rules]]
id="clean.shallow"
include_unverified=true
match='within="." & ref="purge.artifacts"'
""")
        wanted = self.file(self.repo / "recursive/a/b/c/d/e/f/node_modules/data").parent
        outside = self.file(self.repo / "other/a/b/c/d/e/f/unmatched.bin")
        visits = []
        scandir = scan.os.scandir

        def record(path):
            visits.append(path)
            return scandir(path)

        with mock.patch.object(scan.os, "scandir", record):
            report = self.run_rule(rules=("deep", "shallow"))
        self.assertIn(wanted, {c.exact_path for c in report.choices})
        self.assertNotIn(outside.parent, visits)

    def test_sibling_files_reuse_rule_scopes(self):
        self.write_rules("""[[rules]]
id="clean.project"
match='ref="purge.artifacts"'
""")
        self.file(self.repo / "package.json", "{}")
        for index in range(100):
            self.file(self.repo / f"unmatched-{index}.txt")
        with mock.patch.object(scan, "RuleScope", wraps=scan.RuleScope) as scope:
            self.run_rule(rules=("project",), paths=(self.repo,))
        self.assertLessEqual(scope.call_count, 2)

    def test_scope_index_keeps_ancestors_descendants_and_policy_order(self):
        rule = ScanRule(id="purge.test", match=Condition("glob", "*"), source="project")
        base = self.repo
        scopes = tuple(RuleScope(rule, base / f"p{i}") for i in range(2000))
        scopes = (
            RuleScope(rule, base / "p1/sub/child"),
            *scopes,
            RuleScope(rule, base),
        )
        index = matching.ScopeIndex(scopes)
        path = base / "p1/sub"
        expected = matching.relevant_scopes(scopes, path)
        with mock.patch.object(
            Path, "is_relative_to", side_effect=AssertionError("linear scope scan")
        ):
            self.assertEqual(index.for_path(path), expected)

    def test_cross_phase_age_protection_is_checked_before_pruning(self):
        self.repository_policy()
        with self.rules_file.open("a") as stream:
            stream.write(
                '\n[[rules]]\nid="clean.young"\nmin_age_days=30\nmatch=\'within=".local" & glob="**/keep.txt"\'\n'
            )
        keep = self.file(self.repo / ".local/node_modules/keep.txt")
        target = self.file(keep.parent / "drop.bin")
        report = self.run_rule(rules=("project_default", "young"), yes=True)
        self.assertEqual(report.exit_code, 0, report.results)
        self.assertTrue(keep.exists())
        self.assertFalse(target.exists())
