from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from typer.testing import CliRunner

from hagency_cli.cli import app
from hagency_cli.files.cleanup import (
    agents,
    config,
    inspection,
    operations,
    removal,
    scan,
)
from hagency_cli.files.cleanup.models import (
    Activity,
    CleanRequest,
    CleanupDisposition,
    ItemDisposition,
    PurgeRequest,
)
from hagency_cli.files.cleanup.operations import clean_files, purge_space

PROCESS_CHECK = agents.process_block_reason
OCCUPANCY_CHECK = agents.occupancy_block_reason
WHICH = shutil.which


class CleanupUI:
    def __init__(self, *, interactive=False, confirm=True, hook=None):
        self.interactive = interactive
        self.confirm = confirm
        self.hook = hook
        self.confirmed = []
        self.plans = []

    def is_interactive(self):
        return self.interactive

    def present_plan(self, plan):
        self.plans.append(plan)

    def select(self, choices):
        return tuple(choice.id for choice in choices)

    def confirm_exact(self, paths, known_bytes):
        self.confirmed.append(paths)
        if self.hook:
            self.hook()
        return self.confirm


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        # Workspace identity alone is used; the source registry need not parse.
        (self.repo / "hagency-config.toml").write_text("invalid = [")
        self.rules_file = self.repo / "hagency-cleanup.toml"
        self.rules_file.write_text("schema_version = 1\n")
        self.enterContext(mock.patch.object(Path, "home", return_value=self.home))
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                clear=True,
            )
        )
        self.enterContext(
            mock.patch.object(
                config, "_installed_workspace_root", return_value=self.repo
            )
        )
        self.enterContext(
            mock.patch.object(agents, "process_block_reason", return_value=None)
        )
        self.enterContext(
            mock.patch.object(agents, "occupancy_block_reason", return_value=None)
        )
        self.enterContext(mock.patch.object(agents.shutil, "which", return_value=None))

    def write_rules(self, text):
        self.rules_file.write_text("schema_version = 1\n" + text)
        return self.rules_file

    def project(self, relative="project"):
        root = self.base / relative
        root.mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text("{}")
        return root

    def file(self, path, content="payload", *, executable=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if executable:
            path.chmod(0o755)
        return path

    def artifact(self, root, relative="build"):
        path = root / relative
        self.file(path / "payload")
        return path

    def old(self, path, *, days=8, now=None):
        timestamp = (time.time() if now is None else now) - days * 86400
        paths = [path, *path.rglob("*")] if path.is_dir() else [path]
        for child in paths:
            os.utime(child, (timestamp, timestamp), follow_symlinks=False)

    def purge(
        self,
        root,
        *,
        interactive=False,
        hook=None,
        recursive=False,
        dry_run=False,
        include_unverified=False,
    ):
        return purge_space(
            PurgeRequest(
                paths=(root,),
                config=self.rules_file,
                recursive=recursive,
                dry_run=dry_run,
                include_unverified=include_unverified,
            ),
            ui=CleanupUI(interactive=interactive, hook=hook),
        )

    def clean(self, agent="codex", *, interactive=False, hook=None, dry_run=False):
        return clean_files(
            CleanRequest(agents=(agent,), config=self.rules_file, dry_run=dry_run),
            ui=CleanupUI(interactive=interactive, hook=hook),
        )

    def versions(self, agent, versions, current, *, root=None):
        root = root or agents.source_roots(agent + ".versions")[0]
        entries = {}
        for version in versions:
            if agent == "codex":
                entry = root / (version + "-x86_64-unknown-linux-musl")
                self.file(entry / "codex", executable=True)
                self.file(entry / "codex-resources/rg", executable=True)
                self.file(entry / "codex-resources/bwrap", executable=True)
                self.file(entry / "bin/codex", executable=True)
            elif agent == "claude":
                entry = self.file(root / version, executable=True)
            else:
                entry = root / version
                self.file(entry / "app.js")
            entries[version] = entry
        if agent == "codex":
            base = root.parent
            (base / "current").symlink_to(entries[current], target_is_directory=True)
            self.file(base / "install.lock", "")
            launcher = self.home / ".local/bin/codex"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.symlink_to(entries[current] / "bin/codex")
        elif agent == "claude":
            launcher = self.home / ".local/bin/claude"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.symlink_to(entries[current])
        return entries

    def test_discovery_prefers_ancestor_without_parsing_business_config(self):
        child = self.repo / "a/b"
        child.mkdir(parents=True)
        self.write_rules('[[rules]]\nid="purge.artifacts"\npreselect_after_days=11\n')
        snapshot = config.load_rules(cwd=child)
        self.assertEqual(snapshot.files[-1][0], self.rules_file)
        rule = next(rule for rule in snapshot.rules if rule.id == "purge.artifacts")
        self.assertEqual(rule.preselect_after_days, 11)
        self.assertEqual(rule.origin, str(self.rules_file))

    def test_editable_fallback_and_missing_automatic_file_use_builtins(self):
        self.rules_file.unlink()
        snapshot = config.load_rules(cwd=self.base)
        self.assertEqual(snapshot.files[-1], (self.rules_file, None))
        self.assertTrue(any(rule.id == "purge.artifacts" for rule in snapshot.rules))
        with mock.patch.object(config, "_installed_workspace_root", return_value=None):
            standalone = config.load_rules(cwd=self.base)
        self.assertEqual(len(standalone.files), 1)

    def test_nearest_workspace_missing_file_does_not_read_outer_or_editable_rules(self):
        self.write_rules('[[rules]]\nid="purge.artifacts"\nenabled=false\n')
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "hagency-config.toml").write_text("invalid = [")
        snapshot = config.load_rules(cwd=nested)
        self.assertEqual(snapshot.files[-1], (nested / "hagency-cleanup.toml", None))
        self.assertTrue(
            next(
                rule for rule in snapshot.rules if rule.id == "purge.artifacts"
            ).enabled
        )

    def test_scan_target_never_selects_config_and_no_user_config_is_read(self):
        target = self.project()
        artifact = self.artifact(target)
        self.file(target / "hagency-cleanup.toml", "invalid = [")
        self.file(self.home / ".config/hagency/hagency-cleanup.toml", "invalid = [")
        with mock.patch.object(Path, "cwd", return_value=self.repo):
            report = purge_space(
                PurgeRequest(paths=(target,), dry_run=True), ui=CleanupUI()
            )
        self.assertEqual([choice.exact_path for choice in report.choices], [artifact])
        self.assertEqual(report.config_files[-1], self.rules_file)

    def test_explicit_config_is_independent_and_relative_roots_use_its_directory(self):
        external = self.base / "external/rules.toml"
        self.file(
            external,
            '[[rules]]\nid="clean.custom"\ncommand="clean"\nsource="paths"\nroot="cache"\ninclude=["*.tmp"]\n',
        )
        self.file(external.parent / "cache/a.tmp")
        with mock.patch.object(Path, "cwd", return_value=self.repo):
            snapshot = config.load_rules(external)
        self.assertEqual(snapshot.rules[-1].root, external.parent / "cache")
        report = clean_files(CleanRequest(config=external), ui=CleanupUI())
        self.assertEqual(
            [choice.exact_path for choice in report.choices],
            [external.parent / "cache/a.tmp"],
        )

    def test_tilde_root_and_relative_explicit_config(self):
        self.write_rules(
            '[[rules]]\nid="clean.custom"\ncommand="clean"\nsource="paths"\nroot="~/cache"\ninclude=["*.tmp"]\n'
        )
        snapshot = config.load_rules(Path("hagency-cleanup.toml"), cwd=self.repo)
        self.assertEqual(snapshot.rules[-1].root, self.home / "cache")

    def test_invalid_configs_fail_before_scan(self):
        cases = [
            "schema_version=2",
            "schema_version=true",
            "unknown=true",
            "rules={}",
            '[[rules]]\nid="same"\n[[rules]]\nid="same"',
            '[[rules]]\nid="purge.artifacts"\ncommand="clean"',
            '[[rules]]\nid="purge.artifacts"\nsource="paths"',
            '[[rules]]\nid="purge.artifacts"\nroot="cache"',
            '[[rules]]\nid="purge.artifacts"\nkeep_previous=1',
            '[[rules]]\nid="purge.artifacts"\nmin_age_days=-1',
            '[[rules]]\nid="purge.artifacts"\nmin_age_days=' + str(10**400),
            '[[rules]]\nid="purge.artifacts"\npreselect_after_days=1.5',
            '[[rules]]\nid="purge.artifacts"\nenabled="false"',
            '[[rules]]\nid="purge.artifacts"\ninclude="build/"',
            '[[rules]]\nid="purge.artifacts"\ninclude=[1]',
            '[[rules]]\nid="purge.artifacts"\ninclude=["\\\\"]',
            '[[rules]]\nid="purge.artifacts"\nforce=true',
            '[[rules]]\nid="new"\ncommand="clean"\nsource="unknown"\ninclude=[]',
            '[[rules]]\nid="new"\ncommand="clean"\nsource=[]\ninclude=[]',
            '[[rules]]\nid="new"\ncommand="clean"\nsource="paths"\ninclude=[]',
            '[[rules]]\nid="new"\ncommand="clean"\nsource="paths"\nroot="cache"',
            "invalid = [",
        ]
        for contents in cases:
            with self.subTest(contents=contents):
                self.rules_file.write_text(contents)
                with mock.patch.object(
                    scan, "_resolve_roots", side_effect=AssertionError("scanned")
                ):
                    report = self.purge(self.base)
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(report.issues[0].code, "config_error")

    def test_missing_explicit_and_invalid_found_configs_are_errors(self):
        self.rules_file.unlink()
        report = self.purge(self.base)
        self.assertEqual(report.exit_code, 1)
        self.rules_file.write_text("invalid = [")
        with self.assertRaises(config.CleanupConfigError):
            config.load_rules(cwd=self.repo)

    def test_fifo_config_fails_without_blocking(self):
        self.rules_file.unlink()
        os.mkfifo(self.rules_file)
        report = self.purge(self.base)
        self.assertEqual(report.exit_code, 1)
        self.assertIn("regular", report.issues[0].message)

    def test_merge_replaces_arrays_and_disabled_rules_do_not_scan(self):
        root = self.project()
        build = self.artifact(root)
        dist = self.artifact(root, "dist")
        self.write_rules(
            '[[rules]]\nid="purge.artifacts"\ninclude=["build/"]\npreselect_after_days=0\n'
        )
        report = self.purge(root)
        self.assertEqual([choice.exact_path for choice in report.choices], [build])
        self.assertTrue(report.choices[0].preselected)
        self.assertEqual(report.choices[0].rule_ids, ("purge.artifacts",))
        self.assertEqual(report.choices[0].rule_origins, (str(self.rules_file),))
        self.write_rules('[[rules]]\nid="purge.artifacts"\nenabled=false\n')
        self.assertEqual(self.purge(root).choices, ())
        self.assertTrue(dist.exists())

    def test_custom_files_git_ordered_negation_and_permanent_deletion(self):
        root = self.project()
        files = [
            self.file(root / name)
            for name in ("a.tmp", "keep.tmp", "important.tmp", "source.py")
        ]
        self.write_rules("""[[rules]]
id="purge.temp"
command="purge"
source="project"
include=["*.tmp", "!keep.tmp", "important.tmp"]
exclude=["important.tmp", "!important.tmp"]
""")
        report = self.purge(root, interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            {choice.exact_path for choice in report.choices}, {files[0], files[2]}
        )
        self.assertTrue(all(choice.entry_kind == "file" for choice in report.choices))
        self.assertFalse(files[0].exists())
        self.assertTrue(files[1].exists())
        self.assertFalse(files[2].exists())
        self.assertTrue(files[3].exists())

    def test_excluded_descendant_protects_whole_directory_across_rules(self):
        root = self.project()
        build = self.artifact(root)
        keep = self.file(build / "nested/keep.json")
        self.write_rules("""[[rules]]
id="purge.exclusions"
command="purge"
source="project"
include=[]
exclude=["**/build/nested/keep.json"]
[[rules]]
id="purge.parent"
command="purge"
source="project"
include=["build/"]
""")
        report = self.purge(root, interactive=True)
        self.assertEqual(report.choices, ())
        self.assertEqual(report.exit_code, 0)
        self.assertTrue(keep.exists())
        self.assertTrue(
            any("purge.exclusions" in issue.message for issue in report.issues)
        )

    def test_another_rule_cannot_undo_exclusion(self):
        root = self.project()
        artifact = self.artifact(root)
        self.write_rules("""[[rules]]
id="purge.artifacts"
exclude=["build/"]
[[rules]]
id="purge.other"
command="purge"
source="project"
include=["build/"]
exclude=["!build/"]
""")
        self.assertEqual(self.purge(root).choices, ())
        self.assertTrue(artifact.exists())

    def test_overlapping_path_roots_cannot_bypass_excluded_descendant(self):
        keep = self.file(self.repo / "cache/downloads/keep/data")
        self.file(self.repo / "cache/downloads/remove.tmp")
        self.write_rules("""[[rules]]
id="clean.parent"
command="clean"
source="paths"
root="cache"
include=["downloads/"]
[[rules]]
id="clean.child"
command="clean"
source="paths"
root="cache/downloads"
include=["*"]
exclude=["keep/"]
""")
        report = self.clean(interactive=True)
        self.assertTrue(keep.exists())
        self.assertTrue((self.repo / "cache/downloads").exists())
        self.assertFalse((self.repo / "cache/downloads/remove.tmp").exists())
        self.assertEqual(len(report.choices), 1)

    def test_age_and_preselection_have_distinct_strict_boundaries(self):
        root = self.project()
        target = self.artifact(root)
        now = 2_000_000_000
        self.write_rules(
            '[[rules]]\nid="purge.artifacts"\nmin_age_days=7\npreselect_after_days=10\n'
        )
        with mock.patch.object(scan.time, "time_ns", return_value=now * 10**9):
            self.old(target, days=7, now=now)
            self.assertEqual(self.purge(root).choices, ())
            self.old(target, days=8, now=now)
            choice = self.purge(root).choices[0]
            self.assertFalse(choice.preselected)
            self.assertEqual(choice.activity, Activity.RECENT)
            self.old(target, days=10, now=now)
            self.assertFalse(self.purge(root).choices[0].preselected)
            self.old(target, days=10.001, now=now)
            self.assertTrue(self.purge(root).choices[0].preselected)

    def test_stricter_nested_age_rule_protects_parent(self):
        root = self.project()
        target = self.artifact(root)
        self.old(target)
        self.write_rules("""[[rules]]
id="purge.keep-young-payload"
command="purge"
source="project"
include=["build/payload"]
min_age_days=30
""")
        self.assertEqual(self.purge(root).choices, ())

    def test_recursive_removes_default_depth_limit_and_prunes_whole_artifacts(self):
        root = self.project()
        shallow = self.artifact(root, "a/b/c/d/e/build")
        deep = self.artifact(root, "a/b/c/d/e/f/build")
        nested = self.artifact(deep, "dist")
        self.assertEqual({c.exact_path for c in self.purge(root).choices}, {shallow})
        report = self.purge(root, recursive=True)
        self.assertEqual({c.exact_path for c in report.choices}, {shallow, deep})
        self.assertNotIn(nested, [c.exact_path for c in report.choices])

    def test_recursive_prunes_child_mounts(self):
        root = self.project()
        mounted = root / "mounted"
        target = self.artifact(mounted)
        real = os.path.ismount
        with mock.patch.object(
            os.path,
            "ismount",
            side_effect=lambda path: Path(path) == mounted or real(path),
        ):
            self.assertEqual(self.purge(root, recursive=True).choices, ())
        self.assertTrue(target.exists())

    def test_configuration_change_cancels_all_frozen_targets(self):
        root = self.project()
        targets = [self.artifact(root, name) for name in ("build", "dist")]
        report = self.purge(
            root,
            interactive=True,
            hook=lambda: self.rules_file.write_text("schema_version=1\n# changed"),
        )
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(all(target.exists() for target in targets))
        self.assertTrue(any(issue.code == "config_changed" for issue in report.issues))

    def test_automatic_config_appearing_after_preview_stops_deletion(self):
        self.rules_file.unlink()
        root = self.project()
        target = self.artifact(root)
        ui = CleanupUI(
            interactive=True,
            hook=lambda: self.rules_file.write_text("schema_version=1"),
        )
        with mock.patch.object(Path, "cwd", return_value=self.repo):
            report = purge_space(PurgeRequest(paths=(root,)), ui=ui)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(target.exists())

    def test_new_candidate_is_never_added_and_changed_contents_are_skipped(self):
        root = self.project()
        target = self.artifact(root)

        def mutate():
            self.file(target / "new-file")
            self.artifact(root, "dist")

        report = self.purge(root, interactive=True, hook=mutate)
        self.assertEqual(len(report.choices), 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue((target / "new-file").exists())
        self.assertTrue((root / "dist").exists())

    def test_generic_rules_cannot_remove_agent_state_or_versions(self):
        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        state = self.file(self.home / ".codex/auth.json")
        self.write_rules(f'''[[rules]]
id="clean.codex.versions"
enabled=false
[[rules]]
id="clean.bypass"
command="clean"
source="paths"
root="{self.home / ".codex"}"
include=["*"]
''')
        report = self.clean(interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(state.exists())
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_generic_linked_root_and_git_tracked_file_are_protected(self):
        root = self.project()
        self.file(root / "a.tmp")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "a.tmp"], check=True)
        self.write_rules(
            f'[[rules]]\nid="clean.custom"\ncommand="clean"\nsource="paths"\nroot="{root}"\ninclude=["*.tmp"]\n'
        )
        self.assertEqual(self.clean().choices, ())
        link = self.repo / "linked"
        link.symlink_to(root, target_is_directory=True)
        self.write_rules(
            '[[rules]]\nid="clean.custom"\ncommand="clean"\nsource="paths"\nroot="linked"\ninclude=["*.tmp"]\n'
        )
        self.assertEqual(self.clean().exit_code, 1)

    def test_codex_keeps_current_rollback_pending_and_unknown_versions(self):
        versions = self.versions(
            "codex",
            ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0-beta.1"],
            "1.3.0",
        )
        report = self.clean(interactive=True)
        self.assertEqual(report.exit_code, 0, report)
        self.assertEqual(
            {c.exact_path for c in report.choices},
            {versions["1.0.0"], versions["1.1.0"]},
        )
        self.assertTrue(
            all(
                versions[v].exists()
                for v in ("1.2.0", "1.3.0", "1.4.0", "1.5.0-beta.1")
            )
        )

    def test_codex_launcher_and_update_marker_references_remain_protected(self):
        versions = self.versions(
            "codex", ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"], "1.4.0"
        )
        launcher = self.home / ".local/bin/codex"
        launcher.unlink()
        launcher.symlink_to(versions["1.2.0"] / "bin/codex")
        marker = versions["1.0.0"].parent.parent / "auto-update-version"
        marker.write_text(versions["1.0.0"].name)
        report = self.clean(interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_codex_installation_flock_and_directory_lock_prevent_deletion(self):
        import fcntl

        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        base = versions["1.0.0"].parent.parent
        with (base / "install.lock").open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = self.clean(interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(versions["1.0.0"].exists())
        (base / "install.lock.d").mkdir()
        report = self.clean(interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(versions["1.0.0"].exists())

    def test_codex_holds_lock_during_actual_removal(self):
        import fcntl

        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        lock_path = versions["1.0.0"].parent.parent / "install.lock"
        original = operations._permanently_remove

        def remove(candidate):
            with lock_path.open("rb") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            original(candidate)

        with mock.patch.object(operations, "_permanently_remove", side_effect=remove):
            report = self.clean(interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertFalse(versions["1.0.0"].exists())

    def test_agent_current_version_change_is_rechecked(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        launcher = self.home / ".local/bin/claude"

        def switch():
            launcher.unlink()
            launcher.symlink_to(versions["2.0.0"])

        report = self.clean("claude", interactive=True, hook=switch)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue(versions["2.0.0"].exists())

    def test_claude_native_versions_are_file_candidates(self):
        versions = self.versions(
            "claude", ["2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0-beta"], "2.2.0"
        )
        report = self.clean("claude", interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual([c.exact_path for c in report.choices], [versions["2.0.0"]])
        self.assertEqual(report.choices[0].entry_kind, "file")
        self.assertTrue(
            all(versions[v].exists() for v in ("2.1.0", "2.2.0", "2.3.0", "2.4.0-beta"))
        )

    def test_unknown_claude_launcher_retains_all_versions(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        launcher = self.home / ".local/bin/claude"
        launcher.unlink()
        launcher.write_text("#!/bin/sh\nexec something")
        report = self.clean("claude", interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.choices, ())
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_copilot_retention_is_global_across_new_and_legacy_roots(self):
        current = self.versions("copilot", ["1.2.0", "1.3.0", "1.4.0-beta"], "1.3.0")
        legacy_root = self.home / ".copilot/pkg/universal"
        legacy = self.versions(
            "copilot", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0", root=legacy_root
        )
        report = self.clean("copilot", interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            {c.exact_path for c in report.choices}, {legacy["1.0.0"], legacy["1.1.0"]}
        )
        self.assertTrue(current["1.2.0"].exists())
        self.assertTrue(legacy["1.2.0"].exists())
        self.assertTrue(current["1.4.0-beta"].exists())

    def test_overlapping_version_rules_use_strictest_retention(self):
        versions = self.versions(
            "claude", ["2.0.0", "2.1.0", "2.2.0", "2.3.0"], "2.3.0"
        )
        self.write_rules("""[[rules]]
id="clean.keep-more"
command="clean"
source="claude.versions"
include=["*"]
keep_previous=2
""")
        report = self.clean("claude")
        self.assertEqual([c.exact_path for c in report.choices], [versions["2.0.0"]])
        self.assertEqual(
            set(report.choices[0].rule_ids),
            {"clean.claude.versions", "clean.keep-more"},
        )

    def test_codex_home_and_install_directory_environment_overrides(self):
        custom_home = self.home / "custom-codex"
        custom_bin = self.home / "custom-bin"
        with mock.patch.dict(
            os.environ,
            {"CODEX_HOME": str(custom_home), "CODEX_INSTALL_DIR": str(custom_bin)},
        ):
            versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
            self.assertTrue(
                all(path.is_relative_to(custom_home) for path in versions.values())
            )
            custom_bin.mkdir()
            (custom_bin / "codex").symlink_to(versions["1.2.0"] / "bin/codex")
            report = self.clean()
        self.assertEqual([c.exact_path for c in report.choices], [versions["1.0.0"]])

    def test_codex_staging_known_prefixes_and_strict_subtree_age(self):
        self.versions("codex", ["1.0.0"], "1.0.0")
        roots = agents.source_roots("codex.staging")
        staging = [
            self.artifact(root, name)
            for root, name in zip(
                roots,
                (".staging.old", "openai-bundled.staging-old", "marketplace-add-old"),
            )
        ]
        unknown = self.artifact(roots[-1], "other-old")
        now = 2_000_000_000
        for path in [*staging, unknown]:
            self.old(path, days=8, now=now)
        self.old(staging[0], days=7, now=now)
        self.old(staging[1] / "payload", days=1, now=now)
        self.write_rules('[[rules]]\nid="clean.codex.staging"\nmin_age_days=0\n')
        with mock.patch.object(scan.time, "time_ns", return_value=now * 10**9):
            report = self.clean(interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual([c.exact_path for c in report.choices], [staging[2]])
        self.assertTrue(staging[0].exists())
        self.assertTrue(staging[1].exists())
        self.assertTrue(unknown.exists())

    def test_busy_unknown_and_newly_started_agent_are_skipped(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        for reason, exit_code in [
            ("claude is running", 0),
            ("claude process state unknown", 1),
        ]:
            with (
                self.subTest(reason=reason),
                mock.patch.object(agents, "process_block_reason", return_value=reason),
            ):
                report = self.clean("claude", interactive=True)
            self.assertEqual(report.choices, ())
            self.assertEqual(report.exit_code, exit_code)
        with mock.patch.object(
            agents, "process_block_reason", side_effect=[None, "claude is running"]
        ):
            report = self.clean("claude", interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(versions["2.0.0"].exists())

    def test_occupied_staging_is_retained(self):
        staging = self.artifact(
            agents.source_roots("codex.staging")[1], "openai-bundled.staging-old"
        )
        self.old(staging)
        with mock.patch.object(
            agents,
            "occupancy_block_reason",
            return_value="Agent cleanup target is occupied by a process",
        ):
            report = self.clean(interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(staging.exists())

    def test_source_budget_times_out_without_deletion(self):
        self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        with mock.patch.object(agents, "SOURCE_TIMEOUT", 0):
            report = self.clean("claude", interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.choices, ())
        self.assertTrue(any("60 seconds" in issue.message for issue in report.issues))

    def test_clean_preview_does_not_launch_agents_or_installers(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        with mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("launched subprocess")
        ):
            report = self.clean("claude")
        self.assertEqual(report.disposition, CleanupDisposition.PREVIEW)
        self.assertEqual(len(report.choices), 1)
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_clean_agent_filter_and_cli_exit_codes(self):
        self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "file",
                "clean",
                "--agent",
                "claude",
                "--config",
                str(self.rules_file),
                "--dry-run",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("clean.claude.versions", result.output)
        self.assertNotIn("clean.codex.versions", result.output)
        self.assertEqual(
            runner.invoke(app, ["file", "clean", "--agent", "invalid"]).exit_code, 2
        )
        for option in ("--recursive", "-r", "--config"):
            args = ["file", "purge", "--paths", option]
            if option == "--config":
                args.append(str(self.rules_file))
            self.assertEqual(runner.invoke(app, args).exit_code, 2)
        self.rules_file.write_text("bad = [")
        self.assertEqual(
            runner.invoke(
                app, ["file", "clean", "--config", str(self.rules_file)]
            ).exit_code,
            1,
        )
        with mock.patch(
            "hagency_cli.commands.file.clean_files", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(runner.invoke(app, ["file", "clean"]).exit_code, 130)

    def test_non_linux_clean_reports_unsupported_platform(self):
        with mock.patch.object(scan.sys, "platform", "darwin"):
            report = self.clean()
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.choices, ())

    def test_codex_unknown_release_layout_is_retained(self):
        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        (versions["1.0.0"] / "codex-resources/rg").unlink()
        report = self.clean(interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(versions["1.0.0"].exists())

    def test_codex_code_mode_launcher_reference_is_protected(self):
        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        host = self.file(
            versions["1.0.0"] / "bin/codex-code-mode-host", executable=True
        )
        (self.home / ".local/bin/codex-code-mode-host").symlink_to(host)
        self.assertEqual(self.clean().choices, ())

    def test_copilot_unknown_native_launcher_cannot_select_cache_versions(self):
        versions = self.versions("copilot", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        launcher = self.file(
            self.home / "bin/copilot", "\x7fELFunknown", executable=True
        )
        with mock.patch.object(agents.shutil, "which", return_value=str(launcher)):
            report = self.clean("copilot", interactive=True)
        self.assertEqual(report.choices, ())
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_generic_rule_retains_desktop_data(self):
        sentinel = self.file(self.home / ".config/Claude/keep")
        self.write_rules(
            f'[[rules]]\nid="clean.desktop"\ncommand="clean"\nsource="paths"\nroot="{sentinel.parent.parent}"\ninclude=["Claude/"]\n'
        )
        self.assertEqual(self.clean(interactive=True).choices, ())
        self.assertTrue(sentinel.exists())

    def test_hardlinked_file_paths_are_both_removed_but_size_is_counted_once(self):
        root = self.project()
        first = self.file(root / "a.tmp", "x" * 8192)
        second = root / "b.tmp"
        os.link(first, second)
        self.write_rules(
            '[[rules]]\nid="purge.files"\ncommand="purge"\nsource="project"\ninclude=["*.tmp"]\n'
        )
        report = self.purge(root, interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(report.choices), 2)
        self.assertEqual(report.known_bytes, report.choices[0].size_bytes)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_overlapping_project_owners_share_exclusions(self):
        outer = self.project("outer")
        subprocess.run(["git", "init", "-q", str(outer)], check=True)
        inner = self.project("outer/inner")
        target = self.artifact(inner)
        self.write_rules('[[rules]]\nid="purge.artifacts"\nexclude=["inner/build/"]\n')
        report = purge_space(
            PurgeRequest(paths=(outer, inner), config=self.rules_file),
            ui=CleanupUI(interactive=True),
        )
        self.assertEqual(report.choices, ())
        self.assertTrue(target.exists())

    def test_linux_bind_mounts_are_pruned_even_when_ismount_returns_false(self):
        root = self.project()
        mount = root / "bind mount"
        target = self.artifact(mount)
        table = (
            "10 1 0:1 / / rw - tmpfs tmpfs rw\n11 10 0:1 / "
            + str(mount).replace(" ", r"\040")
            + " rw - tmpfs tmpfs rw\n"
        )
        real_open = Path.open

        def open_mountinfo(path, *args, **kwargs):
            if path == Path("/proc/self/mountinfo"):
                return io.StringIO(table)
            return real_open(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", open_mountinfo),
            mock.patch.object(os.path, "ismount", return_value=False),
        ):
            report = self.purge(root, recursive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(target.exists())

    def test_codex_rollback_must_match_current_platform(self):
        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        foreign = versions["1.1.0"].with_name("1.1.0-aarch64-unknown-linux-musl")
        versions["1.1.0"].rename(foreign)
        self.assertEqual(self.clean(interactive=True).choices, ())
        self.assertTrue(versions["1.0.0"].exists())
        self.assertTrue(foreign.exists())

    def test_cancellation_is_zero_even_when_preview_contains_scan_errors(self):
        root = self.project()
        target = self.artifact(root)
        report = purge_space(
            PurgeRequest(paths=(root, root / "missing"), config=self.rules_file),
            ui=CleanupUI(interactive=True, confirm=False),
        )
        self.assertEqual(report.disposition, CleanupDisposition.CANCELLED)
        self.assertEqual(report.exit_code, 0)
        self.assertTrue(target.exists())

    def test_process_detection_uses_native_executable_when_argv_is_uninformative(self):
        executable = self.home / ".local/share/claude/versions/2.0.0"
        executable.parent.mkdir(parents=True)
        shutil.copy2("/bin/sleep", executable)
        process = subprocess.Popen(["runner", "30"], executable=executable)
        real_scandir = os.scandir

        @contextlib.contextmanager
        def only_process(path):
            with real_scandir(path) as entries:
                yield (entry for entry in entries if entry.name == str(process.pid))

        try:
            with mock.patch.object(agents.os, "scandir", side_effect=only_process):
                self.assertEqual(PROCESS_CHECK("claude"), "claude is running")
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_occupancy_detection_sees_current_process_open_file(self):
        target = self.artifact(self.home, "staging")
        real_scandir = os.scandir

        @contextlib.contextmanager
        def only_self(path):
            with real_scandir(path) as entries:
                yield (
                    entry
                    for entry in entries
                    if str(path) != "/proc" or entry.name == str(os.getpid())
                )

        with (
            (target / "payload").open("rb"),
            mock.patch.object(agents.os, "scandir", side_effect=only_self),
        ):
            self.assertIn("occupied", OCCUPANCY_CHECK(target))

    def test_empty_custom_file_is_still_a_cleanup_candidate(self):
        root = self.project()
        target = self.file(root / "empty.tmp", "")
        self.write_rules(
            '[[rules]]\nid="purge.empty"\ncommand="purge"\nsource="project"\ninclude=["*.tmp"]\n'
        )
        report = self.purge(root, interactive=True)
        self.assertEqual(len(report.choices), 1)
        self.assertEqual(report.known_bytes, 0)
        self.assertFalse(target.exists())

    def test_absent_agent_roots_do_not_require_process_access(self):
        with mock.patch.object(
            agents,
            "process_block_reason",
            side_effect=AssertionError("inspected absent agent"),
        ):
            report = clean_files(CleanRequest(config=self.rules_file), ui=CleanupUI())
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.choices, ())

    def test_mount_change_when_opening_candidate_prevents_removal(self):
        root = self.project()
        target = self.artifact(root)

        def mount_id(fd):
            return 2 if Path(os.readlink(f"/proc/self/fd/{fd}")) == target else 1

        with mock.patch.object(
            removal, "_fd_mount_id", side_effect=mount_id, create=True
        ):
            report = self.purge(root, interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue((target / "payload").exists())

    def fake_process(self, argv):
        proc_root = self.base / "proc"
        proc = proc_root / "123"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(argv)
        (proc / "exe").symlink_to("/usr/bin/node")
        real_scandir = os.scandir
        self.enterContext(
            mock.patch.object(
                agents.os,
                "scandir",
                side_effect=lambda path: real_scandir(
                    proc_root if str(path) == "/proc" else path
                ),
            )
        )

    def test_agent_script_after_many_node_flags_blocks_cleanup(self):
        versions = self.versions("copilot", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        args = [b"node", *([b"--flag"] * 12), os.fsencode(versions["1.0.0"] / "app.js")]
        self.fake_process(b"\0".join(args) + b"\0")
        with mock.patch.object(agents, "process_block_reason", PROCESS_CHECK):
            report = self.clean("copilot", interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(versions["1.0.0"].exists())
        self.assertTrue(
            any("copilot is running" in issue.message for issue in report.issues)
        )

    def test_truncated_process_arguments_are_unknown(self):
        self.fake_process(b"node\0" + b"x" * (128 * 1024) + b"\0/copilot/app.js\0")
        self.assertIn("unknown", PROCESS_CHECK("copilot") or "")

    def test_missing_codex_lock_preserves_versions_and_explains_protection(self):
        versions = self.versions("codex", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        lock = versions["1.0.0"].parent.parent / "install.lock"
        lock.unlink()
        report = self.clean(interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.choices, ())
        self.assertTrue(all(path.exists() for path in versions.values()))
        self.assertFalse(lock.exists())
        self.assertTrue(
            any("cannot synchronize" in issue.message for issue in report.issues)
        )

    def test_vanished_eligible_version_keeps_other_candidates(self):
        versions = self.versions(
            "claude", ["2.0.0", "2.1.0", "2.2.0", "2.3.0"], "2.3.0"
        )
        inventory = agents.version_inventory("claude.versions", 1)
        versions["2.1.0"].unlink()
        with mock.patch.object(agents, "version_inventory", return_value=inventory):
            report = self.clean("claude")
        self.assertEqual(
            [choice.exact_path for choice in report.choices], [versions["2.0.0"]]
        )
        self.assertTrue(
            any(issue.code == "candidate_vanished" for issue in report.issues)
        )
        self.assertTrue(versions["2.2.0"].exists())

    def test_unknown_version_state_still_discards_source_candidates(self):
        versions = self.versions(
            "claude", ["2.0.0", "2.1.0", "2.2.0", "2.3.0"], "2.3.0"
        )
        inventory = tuple(
            sorted(
                agents.version_inventory("claude.versions", 1),
                key=lambda entry: entry.path,
            )
        )
        real_lstat = Path.lstat

        def inaccessible(path, *args, **kwargs):
            if path == versions["2.1.0"]:
                raise PermissionError("cannot inspect version")
            return real_lstat(path, *args, **kwargs)

        with (
            mock.patch.object(agents, "version_inventory", return_value=inventory),
            mock.patch.object(Path, "lstat", inaccessible),
        ):
            report = self.clean("claude", interactive=True)
        self.assertEqual(report.choices, ())
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_vanished_rollback_protects_new_fallback_during_revalidation(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        report = self.clean("claude", interactive=True, hook=versions["2.1.0"].unlink)
        self.assertEqual(len(report.choices), 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(versions["2.0.0"].exists())

    def test_purge_plan_uses_one_clock_sample_across_roots(self):
        roots = [self.project(name) for name in ("first", "second")]
        now = 2_000_000_000
        for root in roots:
            self.old(self.artifact(root), days=7, now=now)
        with (
            mock.patch.object(
                scan.time, "time", side_effect=[now, now + 1, now + 2, now + 3]
            ),
            mock.patch.object(
                scan.time, "time_ns", side_effect=[now * 10**9, (now + 1) * 10**9]
            ) as clock,
        ):
            report = purge_space(
                PurgeRequest(paths=tuple(roots), config=self.rules_file), ui=CleanupUI()
            )
        self.assertEqual(len(report.choices), 2)
        self.assertFalse(any(choice.preselected for choice in report.choices))
        clock.assert_called_once_with()

    def test_age_boundary_preserves_nanosecond_precision(self):
        root = self.project()
        target = self.file(root / "old.tmp")
        now = 2_000_000_000
        cutoff = (now - 7 * 86400) * 10**9
        self.write_rules(
            '[[rules]]\nid="purge.ns"\ncommand="purge"\nsource="project"\ninclude=["*.tmp"]\nmin_age_days=7\npreselect_after_days=7\n'
        )
        with (
            mock.patch.object(scan.time, "time", return_value=now),
            mock.patch.object(scan.time, "time_ns", return_value=now * 10**9),
        ):
            for offset in (0, 1, -1):
                with self.subTest(offset=offset):
                    os.utime(target, ns=(cutoff + offset, cutoff + offset))
                    if target.stat().st_mtime_ns != cutoff + offset:
                        self.skipTest(
                            "filesystem does not preserve nanosecond timestamps"
                        )
                    report = self.purge(root)
                    self.assertEqual(len(report.choices), int(offset < 0))
                    if report.choices:
                        self.assertTrue(report.choices[0].preselected)

    def test_empty_cli_scan_paths_are_rejected_before_scanning(self):
        with mock.patch("hagency_cli.commands.file.purge_space") as purge:
            for value in ("", " ", "\t"):
                with self.subTest(value=value):
                    result = CliRunner().invoke(app, ["file", "purge", value])
                    self.assertEqual(result.exit_code, 2, result.output)
            purge.assert_not_called()

    def test_interactive_cli_prints_plan_metadata_once_and_new_errors(self):
        from hagency_cli.commands.cleanup_ui import QuestionaryCleanupUI

        root = self.project()
        target = self.artifact(root)
        self.write_rules('[[rules]]\nid="purge.artifacts"\nexclude=["build/keep"]\n')
        self.file(target / "keep")
        removable = self.artifact(root, "dist")
        with (
            mock.patch.object(
                QuestionaryCleanupUI, "is_interactive", return_value=True
            ),
            mock.patch.object(
                QuestionaryCleanupUI,
                "select",
                side_effect=lambda choices: tuple(choice.id for choice in choices),
            ),
            mock.patch.object(
                QuestionaryCleanupUI,
                "confirm_exact",
                side_effect=lambda *_: self.rules_file.write_text(
                    "schema_version=1\n# changed"
                ),
            ),
        ):
            result = CliRunner().invoke(
                app, ["file", "purge", str(root), "--config", str(self.rules_file)]
            )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(result.output.count(f"Rules: {self.rules_file}"), 1)
        self.assertEqual(result.output.count("Warning [protected]"), 1)
        self.assertIn("config_changed", result.output)
        self.assertTrue(removable.exists())

    def test_unverified_artifact_requires_opt_in_and_manual_selection(self):
        root = self.base / "orphan"
        target = self.artifact(root)
        self.old(target)
        self.assertEqual(self.purge(root).choices, ())
        preview = self.purge(root, include_unverified=True)
        self.assertEqual(len(preview.choices), 1)
        choice = preview.choices[0]
        self.assertFalse(choice.project_verified)
        self.assertFalse(choice.preselected)
        self.assertIn("project context unverified", choice.label)
        self.assertEqual(preview.selected_paths, ())
        self.assertTrue(target.exists())
        dry_run = self.purge(
            root, include_unverified=True, interactive=True, dry_run=True
        )
        self.assertEqual(dry_run.selected_paths, (target,))
        self.assertTrue(target.exists())
        report = self.purge(root, include_unverified=True, interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertFalse(target.exists())

    def test_unverified_custom_files_use_scan_root_patterns_and_age_gate(self):
        root = self.base / "orphan"
        old = self.file(root / "temp/old.tmp")
        keep = self.file(root / "temp/keep.tmp")
        recent = self.file(root / "temp/recent.tmp")
        self.old(old)
        self.old(keep)
        self.write_rules("""[[rules]]
id="purge.files"
command="purge"
source="project"
include=["/temp/*.tmp"]
exclude=["/temp/keep.tmp"]
min_age_days=7
""")
        report = self.purge(root, include_unverified=True, interactive=True)
        self.assertEqual([choice.exact_path for choice in report.choices], [old])
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())
        self.assertTrue(recent.exists())

    def test_unverified_scope_exclusions_protect_nested_project_candidates(self):
        outer = self.base / "container"
        inner = self.project("container/inner")
        target = self.artifact(inner)
        self.file(target / "keep")
        self.write_rules(
            '[[rules]]\nid="purge.artifacts"\nexclude=["/inner/build/keep"]\n'
        )
        report = purge_space(
            PurgeRequest(
                paths=(outer, inner), config=self.rules_file, include_unverified=True
            ),
            ui=CleanupUI(interactive=True),
        )
        self.assertEqual(report.choices, ())
        self.assertTrue(target.exists())
        self.assertTrue(any("excluded" in issue.message for issue in report.issues))

    def test_unverified_opt_in_allows_vendor_bin_but_retains_agent_protection(self):
        root = self.base / "orphan"
        targets = [self.artifact(root, name) for name in ("vendor", "bin")]
        report = self.purge(root, include_unverified=True, interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual({choice.exact_path for choice in report.choices}, set(targets))
        self.assertFalse(any(choice.preselected for choice in report.choices))
        self.assertTrue(all(not path.exists() for path in targets))
        managed = self.artifact(self.home / ".codex")
        report = self.purge(
            self.home / ".codex", include_unverified=True, interactive=True
        )
        self.assertEqual(report.choices, ())
        self.assertTrue(managed.exists())

    def test_unverified_opt_in_retains_git_link_and_mount_protection(self):
        project = self.project()
        root = project / "orphan"
        targets = [self.artifact(root, name) for name in ("build", "vendor", "bin")]
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "orphan"], check=True)
        linked = root / "node_modules"
        linked.symlink_to(self.repo, target_is_directory=True)
        mounted = self.artifact(root, "dist")
        real_mount = scan._is_mount
        with mock.patch.object(
            scan,
            "_is_mount",
            side_effect=lambda path: path == mounted or real_mount(path),
        ):
            report = self.purge(root, include_unverified=True, interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(all(target.exists() for target in targets))
        self.assertTrue(linked.is_symlink())
        self.assertTrue(mounted.exists())

    def test_unverified_opt_in_still_rechecks_project_ownership(self):
        root = self.project()
        target = self.artifact(root)
        report = self.purge(
            root,
            include_unverified=True,
            interactive=True,
            hook=(root / "package.json").unlink,
        )
        self.assertTrue(report.choices[0].project_verified)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(target.exists())
        report = self.purge(
            root,
            include_unverified=True,
            interactive=True,
            hook=lambda: self.file(root / "package.json", "{}"),
        )
        self.assertFalse(report.choices[0].project_verified)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(target.exists())

    def test_unverified_candidate_changes_after_confirmation_are_skipped(self):
        root = self.base / "orphan"
        target = self.artifact(root)
        report = self.purge(
            root,
            include_unverified=True,
            interactive=True,
            hook=lambda: self.file(target / "new-file"),
        )
        self.assertEqual(report.exit_code, 1)
        self.assertTrue((target / "payload").exists())
        self.assertTrue((target / "new-file").exists())

    def test_unverified_duplicate_prefers_verified_project_for_revalidation(self):
        root = self.project()
        nested = root / "nested"
        target = self.artifact(nested)
        report = purge_space(
            PurgeRequest(
                paths=(nested, root), config=self.rules_file, include_unverified=True
            ),
            ui=CleanupUI(interactive=True),
        )
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(report.choices), 1)
        self.assertTrue(report.choices[0].project_verified)
        self.assertFalse(target.exists())

    def test_unverified_cli_flag_help_preview_and_exclusivity(self):
        root = self.base / "orphan"
        target = self.artifact(root)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "file",
                "purge",
                str(root),
                "--include-unverified",
                "--config",
                str(self.rules_file),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("project context unverified", result.output)
        self.assertIn(str(target), result.output)
        self.assertTrue(target.exists())
        result = runner.invoke(
            app, ["file", "purge", "--paths", "--include-unverified"]
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("cannot be combined", result.output)
        self.assertIn(
            "--include-unverified",
            runner.invoke(app, ["file", "purge", "--help"]).output,
        )

    def test_unverified_vendor_bin_in_known_project_have_explicit_reasons(self):
        root = self.project()
        targets = [self.artifact(root, name) for name in ("vendor", "bin")]
        for target in targets:
            self.old(target)
        blocked = self.purge(root)
        self.assertEqual(blocked.choices, ())
        reasons = {issue.path: issue.message for issue in blocked.issues}
        self.assertIn("composer.json", reasons[root / "vendor"])
        self.assertIn(".NET", reasons[root / "bin"])
        report = self.purge(root, include_unverified=True)
        self.assertEqual(len(report.choices), 2)
        for choice in report.choices:
            self.assertTrue(choice.project_verified)
            self.assertFalse(choice.preselected)
            self.assertIn("artifact purpose unverified", choice.label)
        self.assertEqual(report.selected_paths, ())
        self.assertTrue(all(path.exists() for path in targets))
        report = self.purge(root, include_unverified=True, interactive=True)
        self.assertEqual(report.exit_code, 0)
        self.assertTrue(all(not path.exists() for path in targets))

    def test_unverified_vendor_still_respects_exclusions_and_age(self):
        root = self.project()
        vendor = self.artifact(root, "vendor")
        keep = self.file(vendor / "keep")
        self.write_rules('[[rules]]\nid="purge.artifacts"\nexclude=["/vendor/keep"]\n')
        self.assertEqual(
            self.purge(root, include_unverified=True, interactive=True).choices, ()
        )
        self.assertTrue(keep.exists())
        self.write_rules('[[rules]]\nid="purge.artifacts"\nmin_age_days=7\n')
        self.assertEqual(
            self.purge(root, include_unverified=True, interactive=True).choices, ()
        )
        self.assertTrue(vendor.exists())
        self.old(vendor)
        self.assertEqual(
            self.purge(root, include_unverified=True, interactive=True).exit_code, 0
        )
        self.assertFalse(vendor.exists())

    def test_unverified_vendor_evidence_changes_require_new_preview(self):
        root = self.project()
        vendor = self.artifact(root, "vendor")
        composer = root / "composer.json"
        report = self.purge(
            root,
            include_unverified=True,
            interactive=True,
            hook=lambda: self.file(composer, "{}"),
        )
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(vendor.exists())
        report = self.purge(
            root, include_unverified=True, interactive=True, hook=composer.unlink
        )
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(vendor.exists())
        self.assertIn("verification changed", report.results[0].message)

    def test_unverified_opt_in_keeps_global_derived_data_protected(self):
        root = self.project("Library/Developer/Xcode")
        target = self.artifact(root, "DerivedData")
        report = self.purge(root, include_unverified=True, interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(target.exists())
        self.assertTrue(
            any("global Xcode DerivedData" in issue.message for issue in report.issues)
        )

    def test_unverified_vendor_cli_shows_reason_in_readonly_preview(self):
        root = self.project()
        target = self.artifact(root, "vendor")
        result = CliRunner().invoke(
            app,
            [
                "file",
                "purge",
                str(root),
                "--include-unverified",
                "--dry-run",
                "--config",
                str(self.rules_file),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("artifact purpose unverified", result.output)
        self.assertIn("composer.json", result.output)
        self.assertNotIn("Warning [protected]", result.output)
        self.assertTrue(target.exists())

    def test_review_new_path_rules_preselect_only_old_files(self):
        target = self.file(self.repo / "cache/fresh.tmp")
        self.write_rules(
            '[[rules]]\nid="clean.cache"\ncommand="clean"\nsource="paths"\nroot="cache"\ninclude=["*.tmp"]\n'
        )
        report = self.clean()
        self.assertEqual(len(report.choices), 1)
        self.assertFalse(report.choices[0].preselected)
        self.old(target)
        self.assertTrue(self.clean().choices[0].preselected)
        with self.rules_file.open("a") as stream:
            stream.write("preselect_after_days=0\nmin_age_days=1\n")
        self.assertTrue(self.clean().choices[0].preselected)

    def test_review_broad_scan_root_requires_manual_selection(self):
        target = self.artifact(self.project())
        self.old(target)
        report = self.purge(self.base)
        self.assertEqual(len(report.choices), 1)
        self.assertFalse(report.choices[0].preselected)
        self.assertTrue(any(issue.code == "broad_root" for issue in report.issues))
        self.assertTrue(target.exists())

    def test_review_foreign_owned_descendant_protects_whole_candidate(self):
        root = self.project()
        target = self.artifact(root)
        payload = target / "payload"
        real_stat = Path.stat

        def stat_owner(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == payload:
                values = {
                    key: getattr(info, key)
                    for key in dir(info)
                    if key.startswith("st_")
                }
                values["st_uid"] = os.geteuid() + 1
                return SimpleNamespace(**values)
            return info

        with mock.patch.object(Path, "stat", stat_owner):
            report = self.purge(root, interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(any("owner" in issue.message for issue in report.issues))
        self.assertTrue(payload.exists())

    def test_review_missing_frozen_manifest_never_deletes(self):
        root = self.project()
        target = self.artifact(root)
        candidate = scan._build_plan(
            PurgeRequest(paths=(root,), config=self.rules_file)
        ).candidates[0]
        with self.assertRaises(OSError):
            removal._permanently_remove(replace(candidate, tree=()))
        self.assertTrue((target / "payload").exists())
        for frozen in ((), tuple(e for e in candidate.tree if e.relative_path != ".")):
            with self.subTest(frozen=frozen), self.assertRaises(OSError):
                removal._remove_tree_no_follow(target, frozen=frozen)
        self.assertTrue((target / "payload").exists())

    def test_review_launcher_symlink_loop_is_a_reported_source_failure(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        launcher = self.home / ".local/bin/claude"
        launcher.unlink()
        launcher.symlink_to(launcher)
        report = self.clean("claude", interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(issue.code == "source_failed" for issue in report.issues))
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_review_parent_traversal_does_not_bypass_agent_data_protection(self):
        storage = self.repo / "data"
        target = self.file(storage / "auth.json")
        (self.repo / "parent").mkdir()
        self.write_rules(
            '[[rules]]\nid="clean.cache"\ncommand="clean"\nsource="paths"\nroot="data"\ninclude=["*"]\n'
        )
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.repo / "parent/../data")}
        ):
            report = self.clean(interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(any(issue.code == "protected" for issue in report.issues))
        self.assertTrue(target.exists())
        self.assertFalse(inspection._is_within(self.repo / "../outside", self.repo))

    def test_review_symlinked_agent_home_protects_its_real_target(self):
        target = self.file(self.repo / "data/auth.json")
        alias = self.home / ".codex"
        alias.symlink_to(target.parent, target_is_directory=True)
        self.write_rules(
            '[[rules]]\nid="clean.cache"\ncommand="clean"\nsource="paths"\nroot="data"\ninclude=["*"]\n'
        )
        report = self.clean(interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(target.exists())

    def test_review_bidi_descendant_is_protected_and_errors_escape_controls(self):
        from hagency_cli.commands.cleanup_render import render_cleanup_report

        root = self.project()
        target = self.artifact(root)
        payload = self.file(target / "safe\u202etxt")
        report = self.purge(root, interactive=True)
        self.assertEqual(report.choices, ())
        self.assertTrue(payload.exists())
        result = CliRunner().invoke(app, ["file", "purge", str(root / "bad\u202epath")])
        self.assertNotIn("\u202e", result.output)
        self.assertIn(r"\u202e", result.output)
        safe_root = self.project("safe-project")
        self.artifact(safe_root)
        with mock.patch.object(
            operations,
            "_permanently_remove",
            side_effect=OSError("changed: bad\u202ename"),
        ):
            report = self.purge(safe_root, interactive=True)
        output = io.StringIO()
        with (
            contextlib.redirect_stderr(output),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            render_cleanup_report(report)
        self.assertNotIn("\u202e", output.getvalue())
        self.assertIn(r"\u202e", output.getvalue())

    def test_review_config_rejects_del_in_custom_root(self):
        self.write_rules(
            '[[rules]]\nid="clean.cache"\ncommand="clean"\nsource="paths"\nroot="cache\\u007f"\ninclude=["*"]\n'
        )
        with mock.patch.object(scan, "_agent_and_path_plan") as build:
            report = self.clean()
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.issues[0].code, "config_error")
        build.assert_not_called()

    def test_review_whole_directory_removal_does_not_follow_child_symlinks(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                root = self.project(f"project-{fallback}")
                target = self.artifact(root)
                sentinel = self.file(self.repo / f"outside-{fallback}/keep")
                (target / "link").symlink_to(sentinel.parent, target_is_directory=True)
                with mock.patch.object(
                    removal.os,
                    "supports_dir_fd",
                    set() if fallback else os.supports_dir_fd,
                ):
                    report = self.purge(root, interactive=True)
                self.assertEqual(report.exit_code, 0, report)
                self.assertFalse(target.exists())
                self.assertTrue(sentinel.exists())

    def test_review_fifo_child_prevents_deletion(self):
        root = self.project()
        target = self.artifact(root)
        os.mkfifo(target / "pipe")
        report = self.purge(root, interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any(
                "unsupported filesystem entry" in issue.message
                for issue in report.issues
            )
        )
        self.assertTrue((target / "payload").exists())

    def test_review_invalid_selection_never_reaches_confirmation(self):
        root = self.project()
        target = self.artifact(root)
        for unknown in (False, True):
            ui = CleanupUI(interactive=True)
            ui.select = lambda choices, unknown=unknown: (
                ("not-a-candidate",) if unknown else (choices[0].id, choices[0].id)
            )
            report = purge_space(
                PurgeRequest(paths=(root,), config=self.rules_file), ui=ui
            )
            self.assertEqual(report.exit_code, 1)
            self.assertEqual(ui.confirmed, [])
            self.assertTrue(
                any(issue.code == "invalid_selection" for issue in report.issues)
            )
            self.assertTrue(target.exists())

    def test_review_clean_uses_real_process_and_launcher_checks(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        shutil.copy2("/bin/sleep", versions["2.2.0"])
        process = subprocess.Popen([str(versions["2.2.0"]), "30"])
        real_scandir = os.scandir

        @contextlib.contextmanager
        def only_process(path):
            with real_scandir(path) as entries:
                yield (
                    entry
                    for entry in entries
                    if str(path) != "/proc" or entry.name == str(process.pid)
                )

        try:
            with (
                mock.patch.object(agents, "process_block_reason", PROCESS_CHECK),
                mock.patch.object(agents, "occupancy_block_reason", OCCUPANCY_CHECK),
                mock.patch.object(agents.shutil, "which", WHICH),
                mock.patch.dict(os.environ, {"PATH": str(self.home / ".local/bin")}),
                mock.patch.object(os, "scandir", side_effect=only_process),
            ):
                busy = self.clean("claude", interactive=True)
                self.assertEqual(busy.choices, ())
                self.assertTrue(
                    any(issue.code == "agent_busy" for issue in busy.issues)
                )
                self.assertTrue(versions["2.0.0"].exists())
                process.terminate()
                process.wait(timeout=5)
                idle = self.clean("claude", interactive=True)
                self.assertEqual(idle.exit_code, 0, idle)
                self.assertFalse(versions["2.0.0"].exists())
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    def test_review_clean_real_open_descriptor_blocks_removal(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        with versions["2.0.0"].open("rb") as stream:
            process = subprocess.Popen(
                ["/bin/sleep", "30"], pass_fds=(stream.fileno(),)
            )
        real_scandir = os.scandir

        @contextlib.contextmanager
        def only_process(path):
            with real_scandir(path) as entries:
                yield (
                    entry
                    for entry in entries
                    if str(path) != "/proc" or entry.name == str(process.pid)
                )

        try:
            with (
                mock.patch.object(agents, "process_block_reason", PROCESS_CHECK),
                mock.patch.object(agents, "occupancy_block_reason", OCCUPANCY_CHECK),
                mock.patch.object(os, "scandir", side_effect=only_process),
            ):
                report = self.clean("claude", interactive=True)
            self.assertEqual(report.choices, ())
            self.assertTrue(any("occupied" in issue.message for issue in report.issues))
            self.assertTrue(versions["2.0.0"].exists())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_review_real_which_rejects_custom_claude_wrapper_without_launching(self):
        versions = self.versions("claude", ["2.0.0", "2.1.0", "2.2.0"], "2.2.0")
        wrapper = self.file(
            self.repo / "bin/claude", "#!/bin/sh\nexit 99\n", executable=True
        )
        with (
            mock.patch.object(agents.shutil, "which", WHICH),
            mock.patch.dict(os.environ, {"PATH": str(wrapper.parent)}),
            mock.patch.object(
                subprocess, "Popen", side_effect=AssertionError("launched wrapper")
            ),
        ):
            report = self.clean("claude", interactive=True)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any(
                issue.code == "source_failed" and "launcher" in issue.message
                for issue in report.issues
            )
        )
        self.assertTrue(all(path.exists() for path in versions.values()))

    def test_review_config_boundaries_and_empty_override(self):
        self.rules_file.write_text("")
        self.assertEqual(len(config.load_rules(self.rules_file).rules), 6)
        for data in (
            b"\xef\xbb\xbfschema_version=1",
            b"x" * (config.MAX_CONFIG_BYTES + 1),
        ):
            with self.subTest(size=len(data)):
                self.rules_file.write_bytes(data)
                report = self.clean()
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(report.issues[0].code, "config_error")
        self.rules_file.unlink()
        self.rules_file.symlink_to(self.rules_file)
        self.assertEqual(self.clean().issues[0].code, "config_error")

    def test_review_builtin_rules_changes_stop_deletion(self):
        defaults = self.repo / "defaults.toml"
        defaults.write_bytes(config.DEFAULTS_PATH.read_bytes())
        root = self.project()
        target = self.artifact(root)
        with mock.patch.object(config, "DEFAULTS_PATH", defaults):
            report = self.purge(
                root,
                interactive=True,
                hook=lambda: defaults.write_text(defaults.read_text() + "\n# changed"),
            )
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(issue.code == "config_changed" for issue in report.issues))
        self.assertTrue(target.exists())

    def test_review_retained_quarantine_is_not_selected_by_generic_rules(self):
        root = self.project()
        target = self.artifact(root, ".hagency-cleanup-retained")
        self.write_rules(
            '[[rules]]\nid="purge.all"\ncommand="purge"\nsource="project"\ninclude=["*/"]\n'
        )
        self.assertEqual(self.purge(root, interactive=True).choices, ())
        self.assertTrue((target / "payload").exists())

    def test_review_reused_terminal_ui_resets_presented_plan(self):
        from hagency_cli.commands.cleanup_ui import QuestionaryCleanupUI

        ui = QuestionaryCleanupUI()
        ui.presented_plan = mock.sentinel.old_plan
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(ui.is_interactive())
        self.assertIsNone(ui.presented_plan)


if __name__ == "__main__":
    unittest.main()
