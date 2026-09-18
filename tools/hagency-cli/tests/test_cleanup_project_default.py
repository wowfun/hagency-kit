from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.cli import app
from hagency_cli.files.cleanup import config, operations, scan
from hagency_cli.files.cleanup.models import CACHEDIR_TAG_SIGNATURE, CleanRequest
from hagency_cli.files.cleanup.selection import CleanupSelectionError


class ProjectDefaultTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules

    def repository_policy(self):
        policy = Path(__file__).resolve().parents[3] / "hagency-cleanup.toml"
        self.rules_file.write_text(policy.read_text())
        self.file(self.repo / "package.json", "{}")

    def run_rule(
        self,
        *,
        rules=("project_default",),
        paths=None,
        yes=False,
        ui=None,
        recursive=False,
        include_unverified=False,
    ):
        return operations.clean_files(
            CleanRequest(
                config=self.rules_file,
                rules=rules,
                paths=(self.repo,) if paths is None else paths,
                yes=yes,
                dry_run=not yes,
                recursive=recursive,
                include_unverified=include_unverified,
            ),
            ui=ui or fixtures.CleanupUI(),
        )

    def test_repository_policy_boundaries_after_project_purge(self):
        self.repository_policy()
        keep, delete = set(), set()
        for name, size, retained in (
            ("note.txt", 10_000_000, True),
            ("large.md", 10_000_001, False),
            ("photo.JPG", 10_000_000, True),
            ("large.png", 10_000_001, False),
            ("movie.MP4", 50_000_000, True),
            ("large.webm", 50_000_001, False),
            ("unknown.bin", 0, False),
        ):
            path = self.file(self.repo / ".local" / name)
            with path.open("r+b") as stream:
                stream.truncate(size)
            (keep if retained else delete).add(path)
        local_artifact = self.file(self.repo / ".local/node_modules/keep.txt").parent
        local_cache = self.file(
            self.repo / ".local/cache/CACHEDIR.TAG", CACHEDIR_TAG_SIGNATURE.decode()
        ).parent
        self.file(local_cache / "keep.txt")
        keep.add(self.file(self.repo / "unrelated.bin"))
        artifact = self.file(self.repo / "node_modules/dependency.txt").parent
        tagged = self.file(
            self.repo / "cache/CACHEDIR.TAG", CACHEDIR_TAG_SIGNATURE.decode()
        ).parent
        self.file(tagged / "payload")
        report = self.run_rule(yes=True)
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertEqual(
            {choice.exact_path for choice in report.choices},
            delete | {artifact, tagged, local_artifact, local_cache},
        )
        self.assertTrue(all(path.exists() for path in keep))
        self.assertTrue(
            all(
                not path.exists()
                for path in delete | {artifact, tagged, local_artifact, local_cache}
            )
        )
        self.assertTrue((self.repo / ".local").is_dir())

    def test_repository_policy_targets_external_project_from_cli_path(self):
        self.repository_policy()
        root = self.base / "other-project"
        artifact = self.file(root / "node_modules/data").parent
        local = self.file(root / ".local/drop.bin")
        kept = self.file(root / ".local/keep.txt")
        nested = self.file(root / "subproject/.local/untouched.bin")
        config_neighbor = self.file(self.repo / ".local/untouched.bin")
        with mock.patch.object(Path, "cwd", return_value=root):
            result = CliRunner().invoke(
                app,
                [
                    "file",
                    "clean",
                    "-r",
                    "project_default",
                    "./",
                    "--dry-run",
                    "--yes",
                    "--config",
                    str(self.rules_file),
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(artifact), result.output)
        self.assertIn(str(local), result.output)
        report = self.run_rule(paths=(root,), yes=True)
        self.assertEqual(
            {choice.exact_path for choice in report.choices}, {artifact, local}
        )
        self.assertFalse(artifact.exists())
        self.assertFalse(local.exists())
        self.assertTrue(all(path.exists() for path in (kept, nested, config_neighbor)))

    def test_repository_policy_requires_project_path(self):
        self.repository_policy()
        with self.assertRaisesRegex(CleanupSelectionError, "PATH"):
            self.run_rule(paths=())

    def test_repository_policy_local_filter_is_relative_to_each_project_path(self):
        self.repository_policy()
        roots = (self.base / "one", self.base / "two")
        expected = set()
        for root in roots:
            expected.add(self.file(root / ".local/drop.bin"))
            expected.add(self.file(root / "vendor/data").parent)
            self.file(root / ".local/keep.md")
        report = self.run_rule(paths=roots)
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertEqual({choice.exact_path for choice in report.choices}, expected)

    def test_purge_references_keep_project_checks_and_live_revalidation(self):
        self.write_rules(
            '[[rules]]\nid="clean.project"\nmatch={ref="purge.artifacts"}\n'
        )
        root = self.base / "project"
        target = self.file(root / "node_modules/data").parent
        report = self.run_rule(rules=("project",), paths=(root,))
        self.assertEqual(report.choices, ())  # no project evidence
        marker = self.file(root / "package.json", "{}")
        original = operations._build_plan

        def remove_marker(*args, **kwargs):
            plan = original(*args, **kwargs)
            marker.unlink()
            return plan

        with mock.patch.object(operations, "_build_plan", remove_marker):
            report = self.run_rule(rules=("project",), paths=(root,), yes=True)
        self.assertEqual(len(report.choices), 1)
        self.assertTrue(target.exists())
        self.assertIn("project context", report.results[0].message)

    def test_missing_entry_disabled_ref_and_reverse_ref_fail_before_scan(self):
        self.write_rules(
            '[[rules]]\nid="clean.project"\nmatch={ref="purge.artifacts"}\n'
        )
        with mock.patch.object(
            scan, "_scan_root", side_effect=AssertionError("scanned")
        ):
            with self.assertRaisesRegex(CleanupSelectionError, "PATH"):
                self.run_rule(rules=("project",), paths=())
            with self.rules_file.open("a") as stream:
                stream.write('[[rules]]\nid="purge.artifacts"\nenabled=false\n')
            with self.assertRaisesRegex(CleanupSelectionError, "disabled"):
                self.run_rule(rules=("project",), paths=(self.repo,))
        self.write_rules(
            '[[rules]]\nid="purge.reverse"\nmatch={ref="clean.codex.versions"}\n'
        )
        with self.assertRaises(config.CleanupConfigError):
            config.load_rules(self.rules_file)

    def test_repeated_project_references_share_scan_and_keep_depth_six(self):
        self.write_rules("""[[rules]]
id="clean.project"
match={any=[{ref="purge.artifacts"},{ref="purge.artifacts"},{ref="purge.cachedirs"}]}
""")
        self.file(self.repo / "package.json", "{}")
        shallow = self.file(self.repo / "node_modules/data").parent
        boundary = self.file(self.repo / "a/b/c/d/e/node_modules/data").parent
        deep = self.file(self.repo / "a/b/c/d/e/f/node_modules/data").parent
        with mock.patch.object(scan, "_scan_root", wraps=scan._scan_root) as scanning:
            report = self.run_rule(rules=("project",), paths=(self.repo,))
        self.assertEqual(
            {choice.exact_path for choice in report.choices}, {shallow, boundary}
        )
        self.assertTrue(deep.exists())
        self.assertEqual(scanning.call_count, 1)

    def test_project_path_scope_and_git_protection(self):
        self.repository_policy()
        tracked = self.file(self.repo / ".local/tracked.bin")
        target = self.file(self.repo / ".local/delete.bin")
        outside = self.file(self.base / "unselected-project/node_modules/data")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", str(tracked)], check=True)
        report = self.run_rule(paths=(self.repo,), yes=True)
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertFalse(target.exists())
        self.assertTrue(tracked.exists())
        self.assertTrue(outside.exists())

    def test_referenced_exclusion_protects_local_and_artifact_descendants(self):
        self.repository_policy()
        text = self.rules_file.read_text().replace(
            'id = "purge.artifacts"',
            'id = "purge.artifacts"\nexclude = ["**/keep.bin"]',
            1,
        )
        self.rules_file.write_text(text)
        local = self.file(self.repo / ".local/keep.bin")
        artifact = self.file(self.repo / "node_modules/keep.bin")
        report = self.run_rule(yes=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(local.exists())
        self.assertTrue(artifact.exists())

    def test_configured_purge_runs_first_recursively_in_unverified_projects(self):
        self.repository_policy()
        (self.repo / "package.json").unlink()
        artifact = self.file(self.repo / "a/b/c/d/e/f/vendor/data").parent
        remaining = self.file(self.repo / ".local/drop.bin", "x" * 20_000)
        removed = []
        remove = operations._permanently_remove

        def record(item):
            removed.append(item.choice.exact_path)
            remove(item)

        ui = fixtures.CleanupUI(interactive=True)
        with (
            mock.patch.object(
                ui,
                "select",
                side_effect=lambda choices: tuple(c.id for c in reversed(choices)),
            ),
            mock.patch.object(operations, "_permanently_remove", record),
        ):
            report = operations.clean_files(
                CleanRequest(
                    config=self.rules_file,
                    rules=("project_default",),
                    paths=(self.repo,),
                ),
                ui=ui,
            )
        self.assertEqual(report.exit_code, 0, report.results)
        self.assertEqual(removed, [artifact, remaining])
        self.assertEqual(report.selected_paths, (artifact, remaining))

    def test_tagged_local_directory_is_purged_before_media_filter(self):
        self.repository_policy()
        local = self.file(
            self.repo / ".local/CACHEDIR.TAG", CACHEDIR_TAG_SIGNATURE.decode()
        ).parent
        self.file(local / "small.txt")
        report = self.run_rule(yes=True)
        self.assertEqual(report.exit_code, 0, report.results)
        self.assertEqual(report.selected_paths, (local,))
        self.assertFalse(local.exists())

    def test_scan_options_do_not_widen_other_selected_rules(self):
        self.write_rules("""[[rules]]
id="purge.cache"
match={all=[{source="project"},{glob="*.cache/"}]}
[[rules]]
id="clean.loose"
recursive=true
include_unverified=true
match={all=[{ref="purge.cache"},{glob="loose.cache/"}]}
[[rules]]
id="clean.strict"
match={all=[{ref="purge.cache"},{glob="strict.cache/"}]}
""")
        root = self.base / "unverified"
        loose = self.file(root / "a/b/c/d/e/f/loose.cache/data").parent
        strict = self.file(root / "strict.cache/data").parent
        deep = self.file(root / "a/b/c/d/e/f/strict.cache/data").parent
        report = self.run_rule(rules=("loose", "strict"), paths=(root,))
        self.assertEqual({c.exact_path for c in report.choices}, {loose})
        self.file(root / "package.json", "{}")
        report = self.run_rule(rules=("loose", "strict"), paths=(root,))
        self.assertEqual({c.exact_path for c in report.choices}, {loose, strict})
        self.assertTrue(deep.exists())

    def test_scan_option_types_defaults_and_override_inheritance(self):
        for field in ("recursive", "include_unverified"):
            self.write_rules(f'[[rules]]\nid="purge.artifacts"\n{field}=1\n')
            with self.assertRaises(config.CleanupConfigError):
                config.load_rules(self.rules_file)
        self.write_rules(
            '[[rules]]\nid="purge.artifacts"\nrecursive=true\ninclude_unverified=true\n'
        )
        initial = next(
            r
            for r in config.load_rules(self.rules_file).rules
            if r.id == "purge.artifacts"
        )
        inherited = config._rule(
            {"id": initial.id, "exclude": ["keep/**"]}, self.rules_file, initial
        )
        self.assertTrue(inherited.recursive)
        self.assertTrue(inherited.include_unverified)
        override = config._rule(
            {"id": initial.id, "recursive": False}, self.rules_file, initial
        )
        self.assertFalse(override.recursive)
        self.assertTrue(override.include_unverified)

    def test_clean_cli_scan_flags_reach_project_provider(self):
        self.write_rules(
            '[[rules]]\nid="clean.project"\nmatch={ref="purge.artifacts"}\n'
        )
        root = self.base / "unverified"
        target = self.file(root / "a/b/c/d/e/f/vendor/data").parent
        result = CliRunner().invoke(
            app,
            [
                "file",
                "clean",
                str(root),
                "-r",
                "project",
                "-R",
                "--include-unverified",
                "--config",
                str(self.rules_file),
                "--yes",
                "--dry-run",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(target), result.output)
        self.assertTrue(target.exists())
