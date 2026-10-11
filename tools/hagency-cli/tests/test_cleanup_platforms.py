from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from typer.testing import CliRunner

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.cli import app
from hagency_cli.files.cleanup import agents, config, operations, scan
from hagency_cli.files.cleanup.models import CleanRequest, PurgeRequest
from hagency_cli.files.cleanup.selection import select_rules


def metadata(info, **changes):
    values = {
        field: getattr(info, field)
        for field in dir(info)
        if field.startswith("st_") and hasattr(info, field)
    }
    values.update(changes)
    return SimpleNamespace(**values)


class ConfigPlatformTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules

    def test_windows_stable_read_and_snapshot_allow_descriptor_ctime_difference(self):
        fstat = os.fstat

        def descriptor_info(fd):
            info = fstat(fd)
            return metadata(info, st_ctime_ns=info.st_ctime_ns + 1_000_000_000)

        with (
            mock.patch.object(config.sys, "platform", "win32"),
            mock.patch.object(config.os, "fstat", side_effect=descriptor_info),
        ):
            data, signature = config._read(self.rules_file)
            self.assertEqual(data, {"schema_version": 1})
            self.assertIsNotNone(signature)
            snapshot = config.load_rules(self.rules_file)
            self.assertTrue(config.configuration_unchanged(snapshot))

    def test_windows_without_birthtime_does_not_fall_back_to_ctime(self):
        info = self.rules_file.stat()
        path_info = metadata(info)
        vars(path_info).pop("st_birthtime_ns", None)
        descriptor_info = metadata(info, st_ctime_ns=info.st_ctime_ns + 1_000)
        vars(descriptor_info).pop("st_birthtime_ns", None)
        with (
            mock.patch.object(config.sys, "platform", "win32"),
            mock.patch.object(Path, "stat", return_value=path_info),
            mock.patch.object(config.os, "fstat", return_value=descriptor_info),
        ):
            self.assertEqual(config._read(self.rules_file)[0], {"schema_version": 1})

    def test_real_identity_change_during_read_is_rejected_on_windows(self):
        info = self.rules_file.stat()
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_birthtime_ns",
        ):
            with self.subTest(field=field):
                changed = metadata(info, **{field: getattr(info, field, 0) + 1})
                with (
                    mock.patch.object(config.sys, "platform", "win32"),
                    mock.patch.object(
                        config.os, "fstat", side_effect=(info, changed)
                    ),
                    self.assertRaisesRegex(
                        config.CleanupConfigError, "changed while reading"
                    ),
                ):
                    config._read(self.rules_file)

    def test_windows_snapshot_hash_detects_rewrite_with_restored_metadata(self):
        self.rules_file.write_bytes(b"schema_version=1\n")
        with mock.patch.object(config.sys, "platform", "win32"):
            snapshot = config.load_rules(self.rules_file)
            before = self.rules_file.stat()
            self.rules_file.write_bytes(b"schema_version=2\n")
            os.utime(self.rules_file, ns=(before.st_atime_ns, before.st_mtime_ns))
            # Force unchanged path metadata so the content check must catch it.
            stat = Path.stat

            def restored(path, *args, **kwargs):
                return (
                    before
                    if path == self.rules_file
                    else stat(path, *args, **kwargs)
                )

            with mock.patch.object(Path, "stat", restored):
                self.assertFalse(config.configuration_unchanged(snapshot))

    def test_posix_ctime_change_during_read_is_rejected(self):
        info = self.rules_file.stat()
        with (
            mock.patch.object(config.sys, "platform", "linux"),
            mock.patch.object(
                config.os, "fstat",
                side_effect=(info, metadata(info, st_ctime_ns=info.st_ctime_ns + 1)),
            ),
            self.assertRaisesRegex(config.CleanupConfigError, "changed while reading"),
        ):
            config._read(self.rules_file)

    def test_windows_descriptor_ctime_change_during_read_is_rejected(self):
        info = self.rules_file.stat()
        with (
            mock.patch.object(config.sys, "platform", "win32"),
            mock.patch.object(
                config.os, "fstat",
                side_effect=(info, metadata(info, st_ctime_ns=info.st_ctime_ns + 1)),
            ),
            self.assertRaisesRegex(config.CleanupConfigError, "changed while reading"),
        ):
            config._read(self.rules_file)

    def test_windows_unchanged_snapshot_hashes_without_parsing_toml(self):
        with mock.patch.object(config.sys, "platform", "win32"):
            snapshot = config.load_rules(self.rules_file)
            with mock.patch.object(
                config.tomllib, "loads", side_effect=AssertionError("reparsed TOML")
            ):
                self.assertTrue(config.configuration_unchanged(snapshot))


class CleanupPlatformTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules

    def request(self, *rules, yes=False, dry_run=True):
        return CleanRequest(
            paths=(self.repo,),
            config=self.rules_file,
            rules=rules,
            yes=yes,
            dry_run=dry_run,
        )

    def test_agent_provider_requires_a_known_agent_name(self):
        rule = select_rules(
            self.request("codex.versions"), config.load_rules(self.rules_file)
        )[0]
        for source, expected in (
            ("codex.versions", "codex"),
            ("claude.versions", "claude"),
            ("copilot.versions", "copilot"),
            ("codex.staging", "codex"),
            ("project", None),
            ("cachedir-tag", None),
            ("paths", None),
            ("my.custom", None),
        ):
            with self.subTest(source=source):
                self.assertEqual(replace(rule, source=source).agent, expected)

    @unittest.skipUnless(sys.platform == "win32", "native Windows purge test")
    def test_windows_purge_previews_and_deletes_with_git_and_junction_protection(self):
        if not fixtures.WHICH("git"):
            self.skipTest("Git is required for tracked-file protection")
        self.file(self.repo / "package.json", "{}")
        artifact = self.file(self.repo / "node_modules/payload").parent
        cache = self.file(
            self.repo / "cache/CACHEDIR.TAG",
            "Signature: 8a477f597d28d172789f06886806bc55\n",
        ).parent
        self.file(cache / "payload")
        tracked = self.file(self.repo / "dist/keep.txt")
        for arguments in (("init", "--quiet"), ("add", "--", "dist/keep.txt")):
            subprocess.run(
                ["git", "-C", str(self.repo), *arguments],
                capture_output=True,
                check=True,
            )
        outside = self.file(self.base / "outside/keep.bin")
        link = self.repo / "target"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside.parent)],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)

        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                report = operations.purge_space(
                    PurgeRequest(
                        paths=(self.repo,),
                        config=self.rules_file,
                        yes=True,
                        dry_run=dry_run,
                    ),
                    ui=fixtures.CleanupUI(),
                )
                self.assertEqual(report.exit_code, 0, report.issues)
                self.assertEqual(
                    {choice.exact_path for choice in report.choices}, {artifact, cache}
                )
                self.assertTrue(any(
                    issue.code == "protected" and issue.path == tracked.parent
                    for issue in report.issues
                ))
                self.assertEqual(artifact.exists(), dry_run)
                self.assertEqual(cache.exists(), dry_run)
                self.assertTrue(tracked.exists())
                self.assertTrue(outside.exists())
                self.assertTrue(link.exists())

    def test_windows_builtin_only_clean_requires_explicit_supported_rule(self):
        empty = self.repo / "empty"
        empty.mkdir()
        with (
            mock.patch.object(config, "_installed_workspace_root", return_value=None),
            mock.patch.object(scan.sys, "platform", "win32"),
        ):
            snapshot = config.load_rules(cwd=self.base)
            self.assertNotIn("clean.project_default", snapshot.expressions)
            with mock.patch.object(scan, "load_rules", return_value=snapshot):
                automatic = operations.clean_files(
                    CleanRequest(paths=(self.repo,), dry_run=True),
                    ui=fixtures.CleanupUI(),
                )
                self.assertEqual(automatic.exit_code, 1)
                self.assertEqual(automatic.issues[0].code, "unsupported_platform")
                supported = operations.clean_files(
                    CleanRequest(
                        paths=(self.repo,),
                        rules=("empty_directories",),
                        yes=True,
                        dry_run=True,
                    ),
                    ui=fixtures.CleanupUI(),
                )
                self.assertEqual(supported.exit_code, 0, supported.issues)
                self.assertEqual(
                    {choice.exact_path for choice in supported.choices}, {empty}
                )
                self.assertTrue(empty.exists())

    @unittest.skipUnless(sys.platform == "win32", "native Windows purge identity test")
    def test_windows_purge_rejects_zero_inode_roots_subdirectories_and_contents(self):
        self.file(self.repo / "package.json", "{}")
        directory = self.repo / "nested"
        target = self.file(directory / "node_modules/payload.bin")
        stat = Path.stat
        # Inject missing identities into otherwise native Windows filesystem calls.
        for missing_identity in (self.repo, directory, target):
            def zero_inode(path, *args, **kwargs):
                info = stat(path, *args, **kwargs)
                missing = (
                    path.name == target.name if missing_identity == target
                    else path == missing_identity
                )
                return metadata(info, st_ino=0) if missing else info

            with (
                self.subTest(missing_identity=missing_identity),
                mock.patch.object(Path, "stat", zero_inode),
            ):
                report = operations.purge_space(
                    PurgeRequest(paths=(self.repo,), config=self.rules_file, yes=True),
                    ui=fixtures.CleanupUI(),
                )
            self.assertEqual(report.exit_code, 1, report.issues)
            self.assertTrue(any(
                "stable filesystem identity" in issue.message for issue in report.issues
            ))
            self.assertTrue(target.exists())

    def test_windows_zero_inode_directories_each_report_incomplete_scan(self):
        self.write_rules('[[rules]]\nid="clean.files"\nmatch=\'glob="*.bin"\'\n')
        targets = (
            self.file(self.repo / "first/keep.bin"),
            self.file(self.repo / "second/keep.bin"),
        )
        directories = {path.parent for path in targets}
        stat = Path.stat

        def zero_inode(path, *args, **kwargs):
            info = stat(path, *args, **kwargs)
            return metadata(info, st_ino=0) if path in directories else info

        with (
            mock.patch.object(scan.sys, "platform", "win32"),
            mock.patch.object(Path, "stat", zero_inode),
        ):
            report = operations.clean_files(
                self.request("files", yes=True, dry_run=False), ui=fixtures.CleanupUI()
            )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.choices, ())
        self.assertEqual(
            {
                issue.path for issue in report.issues
                if "stable filesystem identity" in issue.message
            },
            directories,
        )
        self.assertTrue(all(path.exists() for path in targets))

    def test_windows_zero_inode_root_fails_closed(self):
        self.write_rules('[[rules]]\nid="clean.files"\nmatch=\'glob="*.bin"\'\n')
        target = self.file(self.repo / "keep.bin")
        stat = Path.stat

        def zero_inode(path, *args, **kwargs):
            info = stat(path, *args, **kwargs)
            return metadata(info, st_ino=0) if path == self.repo else info

        with (
            mock.patch.object(scan.sys, "platform", "win32"),
            mock.patch.object(Path, "stat", zero_inode),
        ):
            report = operations.clean_files(
                self.request("files", yes=True, dry_run=False), ui=fixtures.CleanupUI()
            )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.choices, ())
        self.assertTrue(any(
            "stable filesystem identity" in issue.message for issue in report.issues
        ))
        self.assertTrue(target.exists())

    def test_windows_zero_inode_inside_candidate_prevents_deletion(self):
        self.write_rules('[[rules]]\nid="clean.bundles"\nmatch=\'glob="bundle/"\'\n')
        target = self.file(self.repo / "bundle/payload.bin")
        stat = Path.stat

        def zero_inode(path, *args, **kwargs):
            info = stat(path, *args, **kwargs)
            # Keep the missing identity when removal renames the containing bundle.
            return metadata(info, st_ino=0) if path.name == target.name else info

        with (
            mock.patch.object(scan.sys, "platform", "win32"),
            mock.patch.object(Path, "stat", zero_inode),
        ):
            report = operations.clean_files(
                self.request("bundles", yes=True, dry_run=False),
                ui=fixtures.CleanupUI(),
            )
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(
            "stable filesystem identity" in issue.message for issue in report.issues
        ))
        self.assertTrue(target.exists())

    def test_windows_project_cachedir_and_paths_preview_and_delete(self):
        self.write_rules("""[[rules]]
id="clean.project"
match='ref="purge.artifacts" | ref="purge.cachedirs" | glob="*.log"'
""")
        self.file(self.repo / "package.json", "{}")
        artifact = self.file(self.repo / "node_modules/payload").parent
        cache = self.file(
            self.repo / "cache/CACHEDIR.TAG",
            "Signature: 8a477f597d28d172789f06886806bc55\n",
        ).parent
        self.file(cache / "payload")
        log = self.file(self.repo / "logs/session.log")
        retained = self.file(self.repo / "keep.txt")
        with mock.patch.object(scan.sys, "platform", "win32"):
            compiled = select_rules(
                self.request("project"), config.load_rules(self.rules_file)
            )
            self.assertEqual(
                {rule.source for rule in compiled}, {"project", "cachedir-tag", "paths"}
            )
            preview = operations.clean_files(
                self.request("project", yes=True), ui=fixtures.CleanupUI()
            )
            self.assertEqual(preview.exit_code, 0, preview.issues)
            self.assertEqual(
                {choice.exact_path for choice in preview.choices},
                {artifact, cache, log},
            )
            self.assertTrue(all(path.exists() for path in (artifact, cache, log)))
            deleted = operations.clean_files(
                self.request("project", yes=True, dry_run=False),
                ui=fixtures.CleanupUI(),
            )
            self.assertEqual(deleted.exit_code, 0, deleted.issues)
            self.assertTrue(all(not path.exists() for path in (artifact, cache, log)))
            self.assertTrue(retained.exists())

    def test_windows_agent_and_mixed_rules_stop_before_target_scanning(self):
        self.write_rules("""[[rules]]
id="clean.logs"
match='glob="*.log"'
""")
        target = self.file(self.repo / "session.log")
        cases = (
            (("codex.versions",), "clean.codex.versions"),
            (("logs", "codex.versions"), "clean.codex.versions"),
            (("logs", "auto"), "clean.claude.versions"),
        )
        for rules, unsupported in cases:
            with (
                self.subTest(rules=rules),
                mock.patch.object(scan.sys, "platform", "win32"),
                mock.patch.object(
                    scan, "_project_plan", side_effect=AssertionError("scanned project")
                ),
                mock.patch.object(
                    scan, "_agent_and_path_plan",
                    side_effect=AssertionError("scanned source"),
                ),
                mock.patch.object(
                    agents, "source_roots", side_effect=AssertionError("inspected Agent")
                ),
            ):
                report = operations.clean_files(
                    self.request(*rules, yes=True, dry_run=False),
                    ui=fixtures.CleanupUI(),
                )
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(report.choices, ())
                issue = next(
                    issue for issue in report.issues
                    if issue.code == "unsupported_platform"
                )
                self.assertIn(unsupported, issue.message)
                self.assertIn("Linux/WSL", issue.message)
                self.assertTrue(target.exists())

    def test_unsupported_rule_diagnostic_limits_labels_and_label_length(self):
        names = ("long" + "x" * 500, *(f"agent{index}" for index in range(7)))
        self.write_rules("\n".join(
            f'[[rules]]\nid="clean.{name}"\nmatch=\'source="codex.versions"\'\n'
            for name in names
        ))
        with mock.patch.object(scan.sys, "platform", "win32"):
            report = operations.clean_files(
                self.request(*names), ui=fixtures.CleanupUI()
            )
        self.assertEqual(report.exit_code, 1)
        message = report.issues[0].message
        self.assertLess(len(message), 500)
        self.assertIn("clean.agent3", message)
        self.assertNotIn("clean.agent4", message)
        self.assertIn("3 more", message)

    def test_macos_project_rules_remain_unsupported(self):
        self.write_rules('[[rules]]\nid="clean.logs"\nmatch=\'glob="*.log"\'\n')
        snapshot = config.load_rules(self.rules_file)
        with (
            mock.patch.object(scan.sys, "platform", "darwin"),
            mock.patch.object(scan, "load_rules", return_value=snapshot),
            mock.patch.object(
                scan, "_agent_and_path_plan", side_effect=AssertionError("scanned")
            ),
        ):
            report = operations.clean_files(self.request("logs"), ui=fixtures.CleanupUI())
            self.assertEqual(report.exit_code, 1)
            self.assertEqual(report.issues[0].code, "unsupported_platform")

    def test_cli_help_describes_windows_and_agent_limits(self):
        result = CliRunner().invoke(app, ["file", "clean", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Windows", result.output)
        self.assertIn("Linux/WSL", result.output)

    @unittest.skipUnless(sys.platform == "win32", "native Windows junction test")
    def test_windows_junction_target_is_preserved(self):
        self.write_rules('[[rules]]\nid="clean.files"\nmatch=\'glob="*.bin"\'\n')
        outside = self.file(self.base / "outside/keep.bin")
        control = self.file(self.repo / "remove.bin")
        link = self.repo / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside.parent)],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        report = operations.clean_files(
            self.request("files", yes=True, dry_run=False), ui=fixtures.CleanupUI()
        )
        self.assertEqual(report.exit_code, 0, report.issues)
        self.assertEqual({choice.exact_path for choice in report.choices}, {control})
        self.assertFalse(control.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(link.exists())
