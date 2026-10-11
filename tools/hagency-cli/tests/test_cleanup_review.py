from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.commands.cleanup_render import render_cleanup_rules
from hagency_cli.files.cleanup import (
    conditions,
    config,
    inspection,
    operations,
    planning,
    policy,
    scan,
)
from hagency_cli.files.cleanup.file_filters import is_text_name, parse_file_size
from hagency_cli.files.cleanup.models import CleanRequest, PurgeRequest
from hagency_cli.files.cleanup.selection import select_rules


class CleanupReviewTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules

    def test_project_policy_probe_failure_rejects_only_the_affected_candidate(self):
        self.write_rules("""[[rules]]
id="purge.bundles"
match='source="project" & glob="bundle-*/"'
preselect_after_days=0
[[rules]]
id="purge.retained"
match='source="project" & is_empty_directory'
preselect_after_days=90
[[rules]]
id="clean.project"
match='ref="purge.bundles" | ref="purge.retained"'
""")
        update = planning.PlanningSession.update
        scandir = os.scandir
        for request_type in (CleanRequest, PurgeRequest):
            for failure in ("missing", "unreadable", "timeout"):
                with self.subTest(command=request_type.__name__, failure=failure):
                    root = self.base / request_type.__name__ / failure
                    self.file(root / "package.json", "{}")
                    bad = root / "bundle-bad"
                    leaf = self.file(bad / "child/payload")
                    kept = self.file(bad / "keep.bin")
                    healthy = self.file(root / "bundle-healthy/payload").parent
                    probes = []

                    def change_during_filter(
                        session,
                        stage,
                        path=None,
                        *,
                        bad=bad,
                        failure=failure,
                        leaf=leaf,
                    ):
                        update(session, stage, path)
                        if (
                            stage == "protecting"
                            and path == bad
                            and failure == "missing"
                        ):
                            leaf.unlink()
                            leaf.parent.rmdir()

                    def probe(path, *, leaf=leaf, failure=failure, probes=probes):
                        session = planning.current_session()
                        if (
                            session
                            and session.stage == "protecting"
                            and failure == "timeout"
                        ):
                            probes.append(path)
                            raise inspection.ScanTimeout("protection check timed out")
                        if (
                            session
                            and session.stage == "protecting"
                            and path == leaf.parent
                        ):
                            probes.append(path)
                            if failure == "unreadable":
                                raise PermissionError(
                                    "directory unreadable during protection"
                                )
                        return scandir(path)

                    with (
                        mock.patch.object(
                            planning.PlanningSession, "update", change_during_filter
                        ),
                        mock.patch.object(conditions.os, "scandir", probe),
                    ):
                        report = operations.cleanup(
                            request_type(
                                config=self.rules_file,
                                paths=(root,),
                                rules=("clean.project",)
                                if request_type is CleanRequest
                                else ("purge.bundles", "purge.retained"),
                                yes=True,
                            ),
                            ui=fixtures.CleanupUI(),
                        )
                    self.assertEqual(report.exit_code, 1)
                    self.assertTrue(kept.exists())
                    if failure == "timeout":
                        self.assertEqual(len(probes), 1)
                        self.assertEqual(report.choices, ())
                        self.assertTrue(healthy.exists())
                        self.assertTrue(
                            any(issue.code == "scan_timeout" for issue in report.issues)
                        )
                        self.assertFalse(
                            any(
                                issue.code == "candidate_policy_failed"
                                for issue in report.issues
                            )
                        )
                        continue
                    self.assertEqual(probes, [leaf.parent])
                    self.assertEqual(
                        {choice.exact_path for choice in report.choices}, {healthy}
                    )
                    self.assertFalse(healthy.exists())
                    self.assertTrue(
                        any(
                            issue.code == "candidate_policy_failed"
                            and issue.path == bad
                            for issue in report.issues
                        ),
                        report.issues,
                    )

    @unittest.skipIf(sys.platform == "win32", "Windows requires content rechecks")
    def test_unchanged_config_checks_do_not_read_or_parse_again(self):
        self.write_rules("[[rules]]\nid=\"clean.files\"\nmatch='file_size >= 0'\n")
        snapshot = config.load_rules(self.rules_file)
        with mock.patch.object(config, "_read", side_effect=AssertionError("reread")):
            for _ in range(10):
                self.assertTrue(config.configuration_unchanged(snapshot))

    def test_config_rewrite_with_restored_mtime_and_replacement_are_detected(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.write_rules(
                    "[[rules]]\nid=\"clean.files\"\nmatch='is_text=true'\n"
                )
                snapshot = config.load_rules(self.rules_file)
                before = self.rules_file.stat()
                content = self.rules_file.read_text().replace("files", "other")
                if replacement:
                    other = self.file(self.base / "replacement.toml", content)
                    other.replace(self.rules_file)
                else:
                    self.rules_file.write_text(content)
                os.utime(self.rules_file, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertEqual(self.rules_file.stat().st_size, before.st_size)
                self.assertFalse(config.configuration_unchanged(snapshot))

    def test_windows_config_check_keeps_content_validation(self):
        self.write_rules("[[rules]]\nid=\"clean.files\"\nmatch='file_size >= 0'\n")
        snapshot = config.load_rules(self.rules_file)
        with (
            mock.patch.object(config.sys, "platform", "win32"),
            mock.patch.object(config, "_read", wraps=config._read) as read,
        ):
            self.assertTrue(config.configuration_unchanged(snapshot))
            self.assertEqual(read.call_count, len(snapshot.files))

    def test_missing_optional_config_must_stay_missing(self):
        self.rules_file.unlink(missing_ok=True)
        snapshot = config.load_rules(cwd=self.repo)
        self.assertIn((self.rules_file, None), snapshot.files)
        self.assertTrue(config.configuration_unchanged(snapshot))
        self.rules_file.write_text("schema_version=1\n")
        self.assertFalse(config.configuration_unchanged(snapshot))

    def test_optional_config_becoming_dangling_symlink_is_detected(self):
        self.rules_file.unlink()
        snapshot = config.load_rules(cwd=self.repo)
        try:
            self.rules_file.symlink_to(self.base / "absent-target")
        except OSError as exc:
            if sys.platform == "win32" and exc.winerror == 1314:
                self.skipTest("Windows symlink creation privilege is unavailable")
            raise
        self.assertFalse(config.configuration_unchanged(snapshot))

    def test_config_change_during_revalidation_prevents_removal(self):
        target = self.file(self.base / "cache/file.bin")
        self.write_rules("[[rules]]\nid=\"clean.files\"\nmatch='file_size >= 0'\n")
        revalidate = operations._revalidate

        def change_after_revalidation(item):
            result = revalidate(item)
            self.rules_file.write_text(self.rules_file.read_text() + "enabled=false\n")
            return result

        with mock.patch.object(operations, "_revalidate", change_after_revalidation):
            report = operations.clean_files(
                CleanRequest(
                    paths=(target.parent,),
                    rules=("files",),
                    config=self.rules_file,
                    yes=True,
                ),
                ui=fixtures.CleanupUI(),
            )
        self.assertTrue(target.exists())
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(issue.code == "config_changed" for issue in report.issues))

    def test_deletion_refreshes_mount_table_per_candidate_not_per_entry(self):
        root = self.base / "bundles"
        for name in ("one", "two"):
            for index in range(5):
                self.file(root / name / f"{index}.bin")
        self.write_rules('[[rules]]\nid="clean.bundles"\nmatch=\'glob="*/"\'\n')
        mounted = set()
        removed = []
        remove = operations._permanently_remove

        def remove_then_mount_next(item):
            remove(item)
            removed.append(item.choice.exact_path)
            mounted.update(path for path in root.iterdir() if path.is_dir())

        with (
            mock.patch.object(
                inspection, "_mount_points", side_effect=lambda: frozenset(mounted)
            ) as mounts,
            mock.patch.object(
                operations, "_permanently_remove", remove_then_mount_next
            ),
        ):
            report = operations.clean_files(
                CleanRequest(
                    paths=(root,), rules=("bundles",), config=self.rules_file, yes=True
                ),
                ui=fixtures.CleanupUI(),
            )
        self.assertEqual(len(report.choices), 2)
        self.assertEqual(len(removed), 1)
        self.assertEqual(mounts.call_count, 3)  # planning, first delete, second delete
        self.assertEqual(len(list(root.iterdir())), 1)

    def test_fixed_text_names_do_not_classify_every_lock_or_dockerfile_variant(self):
        for name in ("yarn.lock", "Gemfile.lock", "composer.lock", "Dockerfile.dev"):
            with self.subTest(name=name):
                self.assertFalse(is_text_name(Path(name)))
        for name in ("Cargo.lock", "POETRY.LOCK", "uv.lock", "Dockerfile"):
            self.assertTrue(is_text_name(Path(name)))

    def test_size_parser_preserves_long_zero_padding_and_exact_binary_fraction(self):
        for text in (
            "0" * 100_000 + "1B",
            "1." + "0" * 100_000 + "B",
            "0.0000000000009094947017729282379150390625TiB",
        ):
            self.assertEqual(parse_file_size(text), 1)
        for text in ("9" * 100_000 + "B", "0." + "0" * 100_000 + "1TiB"):
            with self.assertRaises(ValueError):
                parse_file_size(text)

    def test_loaded_snapshot_reuses_one_expansion_for_selection_and_query(self):
        self.write_rules("""[[rules]]
id="clean.files"
match='file_size >= 0'
""")
        with mock.patch.object(
            config, "expand_rules", wraps=conditions.expand_rules
        ) as expand:
            snapshot = config.load_rules(self.rules_file)
            self.assertEqual(expand.call_count, 1)
        with mock.patch.object(
            conditions, "expand_rules", side_effect=AssertionError("expanded again")
        ):
            selected = select_rules(
                CleanRequest(paths=(self.base,), rules=("files",)), snapshot
            )
            self.assertIs(selected[0].match, snapshot.expressions["clean.files"])
            with mock.patch("builtins.print"):
                render_cleanup_rules(snapshot.rules, resolved=snapshot.expressions)
        with self.assertRaises(TypeError):
            snapshot.expressions["clean.files"] = conditions.Condition("glob", "*")

    def test_equal_protection_limits_need_no_descendant_condition_evaluation(self):
        self.file(self.base / "cache/bundle/data.bin")
        self.write_rules("""[[rules]]
id="clean.whole"
match='glob="bundle/"'
[[rules]]
id="clean.same-age"
match='file_size >= 0'
""")
        plan = scan._build_plan(
            CleanRequest(
                paths=(self.base / "cache",),
                config=self.rules_file,
                rules=("whole", "same-age"),
            )
        )
        candidate = plan.candidates[0]
        with mock.patch.object(
            policy, "applicable_policies", wraps=policy.applicable_policies
        ) as matching:
            policy.age_policy(
                candidate.choice.exact_path,
                list(candidate.tree),
                candidate.scopes,
                candidate.exclusions,
                2_000_000_000 * 10**9,
            )
        self.assertEqual(matching.call_count, 0)

    def test_sibling_directory_candidates_share_ancestors_but_discover_nested_git(self):
        root = self.base / "repo-files"
        first = self.file(root / "first/payload").parent
        second = self.file(root / "second/nested/payload").parent.parent
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "init", "-q", str(second / "nested")], check=True)
        with planning.planning_session(0, None) as session:
            with mock.patch.object(
                inspection,
                "_git_context_from_marker",
                wraps=inspection._git_context_from_marker,
            ) as marker:
                one, error = session.git_contexts(first)
                self.assertIsNone(error)
                marker.reset_mock()
                two, error = session.git_contexts(second)
                self.assertIsNone(error)
                self.assertNotIn(mock.call(root / ".git"), marker.call_args_list)
            self.assertEqual({context.root for context in one}, {root})
            self.assertEqual(
                {context.root for context in two}, {root, second / "nested"}
            )
        # Live revalidation must discover a new marker, independently of planning.
        subprocess.run(["git", "init", "-q", str(first)], check=True)
        live, error = inspection._discover_git_contexts(first)
        self.assertIsNone(error)
        self.assertEqual({context.root for context in live}, {root, first})

    def test_git_index_keeps_only_files_and_queries_directory_prefixes(self):
        root = self.base / "large"
        root.mkdir()
        names = [f"group{i}/a/b/c/d/e/f/file.bin" for i in range(2000)] + [
            "group1-other.bin"
        ]
        output = b"\0".join(os.fsencode(name) for name in names) + b"\0"
        with (
            planning.planning_session(0, None) as session,
            mock.patch.object(
                planning.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, output, b""),
            ),
        ):
            self.assertEqual(session.git_tracked(root / "group1", root), (True, None))
            self.assertEqual(session.git_tracked(root / names[-1], root), (True, None))
            self.assertEqual(
                session.git_tracked(root / "group1-absent", root), (False, None)
            )
            self.assertEqual(session.git_tracked(root, root), (True, None))
            index, error = session.tracked[root]
            self.assertIsNone(error)
            self.assertEqual(len(index), len(names))

    def test_walk_visits_shared_nodes_by_identity_without_graph_comparison(self):
        first = conditions.Condition("glob", "*")
        second = conditions.Condition("glob", "*")
        root = conditions.Condition("any", children=(first, second, first))
        with mock.patch.object(
            conditions.Condition,
            "__eq__",
            side_effect=AssertionError("graph comparison"),
        ):
            self.assertEqual(
                [id(node) for node in conditions.walk(root)],
                [id(root), id(first), id(second)],
            )

    def test_expired_git_budget_never_spawns_or_caches_failure(self):
        with (
            planning.planning_session(0, None) as session,
            inspection.mount_check_scope(),
        ):
            with (
                inspection.scan_budget(0),
                mock.patch.object(planning.subprocess, "run") as run,
            ):
                with self.assertRaises(inspection.ScanTimeout):
                    session.git_tracked(self.base / "file", self.base)
                run.assert_not_called()
            self.assertEqual(session.tracked, {})

    def test_git_budget_expiring_during_environment_setup_does_not_spawn(self):
        for backend in ("planning", "revalidation"):
            now = [0.0]

            class SlowEnvironment(dict):
                clock = now

                def items(self):
                    self.clock[0] = 2.0
                    return super().items()

            with (
                self.subTest(backend=backend),
                mock.patch.object(planning.os, "environ", SlowEnvironment()),
                mock.patch.object(
                    planning.time, "monotonic", side_effect=lambda now=now: now[0]
                ),
                mock.patch.object(
                    planning.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, b"", b""),
                ) as run,
            ):
                token = inspection._DEADLINE.set(1.0)
                try:
                    session = planning.PlanningSession(0, None)
                    query = (
                        session.git_tracked
                        if backend == "planning"
                        else inspection._git_tracked_state
                    )
                    with self.assertRaises(inspection.ScanTimeout):
                        query(self.base / "file", self.base)
                    run.assert_not_called()
                finally:
                    inspection._DEADLINE.reset(token)
