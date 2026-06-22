from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tomllib

from hagency_cli import cli
from hagency_cli import common as common_module
from hagency_cli import profiles as profile_module
from hagency_cli import sources as source_module


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profiles" / "content").mkdir(parents=True)
        self.config_path = self.root / "hagency-config.toml"
        self.config_path.write_text(
            textwrap.dedent(
                """
                [defaults]
                checkout_dir = "checkouts"

                [source.local-source]
                path = "local-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (self.root / "profiles" / "content" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "content"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.write_skill(self.root / "skills" / "local-one")
        self.write_skill(self.root / "local-source" / "nested" / "external-one")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_skill(self, path: Path, name: str | None = None) -> None:
        path.mkdir(parents=True, exist_ok=True)
        skill_name = name or path.name
        (path / "SKILL.md").write_text(
            textwrap.dedent(
                f"""
                ---
                name: {skill_name}
                description: Test skill.
                ---

                Test body.
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def run_cli(self, *args: str, cwd: Path | None = None, expected: int = 0) -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_argv = sys.argv
        old_cwd = Path.cwd()
        sys.argv = ["hagency", *args]
        code = 0
        try:
            os.chdir(cwd or self.root)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.chdir(old_cwd)
            sys.argv = old_argv
        self.assertEqual(code, expected, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def read_config(self) -> dict:
        with self.config_path.open("rb") as handle:
            return tomllib.load(handle)

    def read_profile(self, name: str = "content") -> dict:
        with (self.root / "profiles" / name / "config.toml").open("rb") as handle:
            return tomllib.load(handle)

    def append_remote_source(self, name: str = "remote-source") -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""

                    [source.{name}.remote]
                    url = "https://example.invalid/acme/ExamplePack.git"
                    """
                )
            )

    def append_local_source(self, name: str) -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""

                    [source.{name}]
                    path = "{name}"
                    """
                )
            )
        (self.root / name).mkdir()

    def test_init_creates_config_and_refuses_existing_without_force(self) -> None:
        new_root = self.root / "new-workspace"
        stdout, _stderr = self.run_cli("init", "--root", str(new_root), cwd=self.root)
        self.assertIn("initialized hagency workspace:", stdout)
        self.assertTrue((new_root / "hagency-config.toml").exists())
        with (new_root / "hagency-config.toml").open("rb") as handle:
            self.assertEqual(tomllib.load(handle)["defaults"]["depth"], 1)

        _stdout, stderr = self.run_cli("init", "--root", str(new_root), expected=1)
        self.assertIn("workspace config already exists", stderr)

        stdout, _stderr = self.run_cli("init", "--root", str(new_root), "--force", "--dry-run")
        self.assertIn("Would overwrite workspace config:", stdout)

    def test_workspace_discovery_and_root_override(self) -> None:
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)

        stdout, _stderr = self.run_cli("source", "list", cwd=nested)
        self.assertIn("local-source\tlocal", stdout)

        other = self.root / "other"
        other.mkdir()
        (other / "hagency-config.toml").write_text(
            textwrap.dedent(
                """
                [source.other-source]
                path = "other-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        stdout, _stderr = self.run_cli("source", "list", "--root", str(other), cwd=nested)
        self.assertIn("other-source\tlocal", stdout)
        self.assertNotIn("local-source\tlocal", stdout)

    def test_windows_git_bash_path_normalization(self) -> None:
        with mock.patch.object(common_module.os, "name", "nt"):
            self.assertEqual(common_module.normalize_windows_shell_path("/c/Users/me/project"), "C:/Users/me/project")
            self.assertEqual(common_module.normalize_windows_shell_path("/d"), "D:/")
            self.assertEqual(common_module.normalize_windows_shell_path(r"C:\Users\me\project"), r"C:\Users\me\project")

        self.assertEqual(common_module.normalize_windows_shell_path("/c/Users/me/project"), "/c/Users/me/project")

    def test_dry_run_command_output_is_shell_neutral(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            result = common_module.run(["git", "status"], cwd=self.root, dry_run=True)

        output = stdout.getvalue()
        self.assertIsNone(result)
        self.assertIn(f"+ cwd: {self.root}", output)
        self.assertIn("+ cmd: git status", output)
        self.assertNotIn("&&", output)

    def test_source_add_list_show_remove_use_generic_schema(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "example-pack",
            "--url",
            "https://example.invalid/acme/ExamplePack.git",
            "--ref",
            "main",
        )
        self.assertIn("added source: example-pack", stdout)
        added = self.read_config()["source"]["example-pack"]
        self.assertEqual(added["remote"]["url"], "https://example.invalid/acme/ExamplePack.git")
        self.assertNotIn("skills_path", added)

        stdout, _stderr = self.run_cli("source", "list")
        self.assertIn("name\ttype\tpath\turl\tref", stdout)
        self.assertIn("example-pack\tremote", stdout)
        stdout, _stderr = self.run_cli("source", "ls")
        self.assertIn("example-pack\tremote", stdout)

        stdout, _stderr = self.run_cli("source", "show", "example-pack")
        self.assertIn("name: example-pack", stdout)
        self.assertIn("remote.ref: main", stdout)
        self.assertNotIn("skills_path", stdout)

        stdout, _stderr = self.run_cli("source", "rm", "example-pack")
        self.assertIn("removed source: example-pack", stdout)
        self.assertNotIn("example-pack", self.read_config()["source"])

    def test_source_top_level_short_alias(self) -> None:
        stdout, _stderr = self.run_cli("s", "ls")

        self.assertIn("local-source\tlocal", stdout)

    def test_source_add_url_positional_infers_name(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git",
        )

        self.assertIn("added source: ExamplePack", stdout)
        added = self.read_config()["source"]["ExamplePack"]
        self.assertEqual(added["remote"]["url"], "https://example.invalid/acme/ExamplePack.git")

    def test_source_add_url_positional_strips_trailing_slash_and_git_suffix(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git/",
        )

        self.assertIn("added source: ExamplePack", stdout)
        self.assertIn("ExamplePack", self.read_config()["source"])

    def test_source_add_scp_style_url_positional_infers_name(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "git@example.invalid:acme/ExamplePack.git",
        )

        self.assertIn("added source: ExamplePack", stdout)
        added = self.read_config()["source"]["ExamplePack"]
        self.assertEqual(added["remote"]["url"], "git@example.invalid:acme/ExamplePack.git")

    def test_source_add_url_positional_name_override(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git",
            "--name",
            "example-pack-alt",
        )

        self.assertIn("added source: example-pack-alt", stdout)
        self.assertIn("example-pack-alt", self.read_config()["source"])

    def test_source_add_sync_dry_run_prints_added_source_sync(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git",
            "--sync",
            "--dry-run",
        )

        self.assertIn("Would add source:", stdout)
        self.assertIn("[source.ExamplePack.remote]", stdout)
        self.assertIn("sync source [1/1] ExamplePack", stdout)
        self.assertIn(
            "git clone --origin origin --branch main --depth 1 https://example.invalid/acme/ExamplePack.git",
            stdout,
        )
        self.assertNotIn("ExamplePack", self.read_config().get("source", {}))

    def test_source_add_sync_writes_config_and_syncs_added_source(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        with mock.patch.object(cli, "sync_source") as sync_mock:
            stdout, _stderr = self.run_cli(
                "source",
                "add",
                "https://example.invalid/acme/ExamplePack.git",
                "--sync",
            )

        self.assertIn("added source: ExamplePack", stdout)
        self.assertIn("sync source [1/1] ExamplePack", stdout)
        self.assertIn("ExamplePack", self.read_config()["source"])
        sync_mock.assert_called_once()
        source_arg = sync_mock.call_args.args[0]
        self.assertEqual(source_arg.name, "ExamplePack")
        self.assertEqual(sync_mock.call_args.kwargs["dry_run"], False)
        self.assertEqual(sync_mock.call_args.kwargs["depth"], 1)

    def test_source_add_url_positional_keeps_basename_when_no_conflict(self) -> None:
        stdout, _stderr = self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        self.assertIn("added source: skills", stdout)
        self.assertIn("skills", self.read_config()["source"])

    def test_source_add_inferred_name_conflict_uses_owner_prefixed_name(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        stdout, _stderr = self.run_cli("source", "add", "https://example.invalid/mattpocock/skills.git")

        self.assertIn("added source: mattpocock/skills", stdout)
        added = self.read_config()["source"]["mattpocock/skills"]
        self.assertEqual(added["remote"]["url"], "https://example.invalid/mattpocock/skills.git")

    def test_source_add_scp_style_inferred_name_conflict_uses_owner_prefixed_name(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        stdout, _stderr = self.run_cli("source", "add", "git@example.invalid:mattpocock/skills.git")

        self.assertIn("added source: mattpocock/skills", stdout)
        added = self.read_config()["source"]["mattpocock/skills"]
        self.assertEqual(added["remote"]["url"], "git@example.invalid:mattpocock/skills.git")

    def test_source_add_inferred_owner_prefixed_conflict_fails_with_custom_name_hint(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")
        self.run_cli("source", "add", "https://example.invalid/mattpocock/skills.git")

        _stdout, stderr = self.run_cli("source", "add", "https://example.invalid/mattpocock/skills", expected=1)

        self.assertIn("source already exists: skills", stderr)
        self.assertIn("owner-prefixed source also exists: mattpocock/skills", stderr)
        self.assertIn("pass --name <custom-name>", stderr)

    def test_source_add_explicit_name_conflict_does_not_fallback(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        _stdout, stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/mattpocock/skills.git",
            "--name",
            "skills",
            expected=1,
        )

        self.assertIn("source already exists: skills", stderr)
        self.assertNotIn("mattpocock/skills", self.read_config()["source"])

    def test_source_add_inferred_name_conflict_fails_when_owner_name_cannot_be_inferred(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/skills.git")

        _stdout, stderr = self.run_cli("source", "add", "https://example.invalid/skills", expected=1)

        self.assertIn("source already exists: skills", stderr)
        self.assertIn("could not infer owner/repo name from URL", stderr)
        self.assertIn("pass --name <custom-name>", stderr)

    def test_source_add_inferred_names_remain_case_sensitive(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/acme/ExamplePack.git")

        stdout, _stderr = self.run_cli("source", "add", "https://example.invalid/acme/example-pack")
        self.assertIn("added source: example-pack", stdout)

    def test_source_sync_default_depth_config_applies_to_clone_dry_run(self) -> None:
        self.append_remote_source()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("source", "sync", "remote-source", "--dry-run")

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertNotIn("&&", stdout)
        self.assertIn(
            "git clone --origin origin --branch main --depth 1 https://example.invalid/acme/ExamplePack.git",
            stdout,
        )

    def test_source_sync_depth_flag_overrides_default_depth_config(self) -> None:
        self.append_remote_source()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("source", "sync", "remote-source", "--depth", "2", "--dry-run")

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertIn(
            "git clone --origin origin --branch main --depth 2 https://example.invalid/acme/ExamplePack.git",
            stdout,
        )

    def test_source_sync_depth_rejects_non_positive_values(self) -> None:
        _stdout, stderr = self.run_cli("source", "sync", "--depth", "0", "--dry-run", expected=2)
        self.assertIn("must be a positive integer", stderr)

    def test_source_sync_default_depth_config_rejects_non_positive_values(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 0',
            ),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("source", "sync", "--dry-run", expected=1)

        self.assertIn("defaults.depth must be a positive integer", stderr)

    def test_source_slice_parsing_valid_values(self) -> None:
        self.assertEqual(cli.parse_source_slice("4:", 5), [4, 5])
        self.assertEqual(cli.parse_source_slice("2:4", 5), [2, 3, 4])
        self.assertEqual(cli.parse_source_slice(":3", 5), [1, 2, 3])
        self.assertEqual(cli.parse_source_slice("4", 5), [4])
        self.assertEqual(cli.parse_source_slice("1,3", 5), [1, 3])
        self.assertEqual(cli.parse_source_slice("1,3:", 5), [1, 3, 4, 5])

    def test_source_slice_parsing_invalid_values(self) -> None:
        for value in ["0", "-1", "4:2", "abc", "1:2:3", "6", "1,,3", ",1", "1,"]:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_source_slice(value, 5)

    def test_source_sync_slice_dry_run_uses_original_indexes(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        stdout, _stderr = self.run_cli("source", "sync", "--dry-run", "-s", "2:3")

        self.assertNotIn("sync source [1/3] local-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_slice_accepts_jumping_indexes(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        stdout, _stderr = self.run_cli("source", "sync", "--dry-run", "-s", "1,3")

        self.assertIn("sync source [1/3] local-source", stdout)
        self.assertNotIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_slice_accepts_jumping_index_plus_tail(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        stdout, _stderr = self.run_cli("source", "sync", "--dry-run", "-s", "1,3:")

        self.assertIn("sync source [1/3] local-source", stdout)
        self.assertNotIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_profile_slice_applies_after_profile_selection(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")
        (self.root / "profiles" / "content" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]

                [skill.second-source]

                [skill.third-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("source", "sync", "--profile", "content", "-s", "2:", "--dry-run")

        self.assertNotIn("sync source [1/3] local-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_failure_continues_and_summarizes_without_traceback(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        def fake_sync(source, *, dry_run: bool, depth: int | None = None) -> None:
            if source.name == "second-source":
                raise subprocess.CalledProcessError(128, ["git", "fetch", "origin"])

        with mock.patch.object(cli, "sync_source", side_effect=fake_sync):
            stdout, stderr = self.run_cli("source", "sync", expected=1)

        self.assertIn("sync source [1/3] local-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)
        self.assertIn("source second-source failed", stderr)
        self.assertIn("source sync failed for: second-source", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_git_fetch_uses_hardcoded_retries(self) -> None:
        source_path = self.root / "checkout"
        source_path.mkdir()
        source = source_module.Source(
            name="remote-source",
            path=source_path,
            remote=source_module.Remote(
                name="origin",
                url="https://example.invalid/acme/ExamplePack.git",
                ref="main",
            ),
        )
        current_remote = subprocess.CompletedProcess(["git"], 0, "https://example.invalid/acme/ExamplePack.git\n", "")
        calls: list[list[str]] = []

        def fail_run(cmd, *, cwd=None, dry_run: bool = False):
            calls.append(cmd)
            raise subprocess.CalledProcessError(128, cmd)

        stdout = io.StringIO()
        with (
            mock.patch.object(source_module, "git_ok", return_value=True),
            mock.patch.object(source_module.subprocess, "run", return_value=current_remote),
            mock.patch.object(source_module, "run", side_effect=fail_run),
            mock.patch.object(source_module.time, "sleep"),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            source_module.sync_source(source, dry_run=False, depth=1)

        fetch_calls = [cmd for cmd in calls if cmd[:2] == ["git", "fetch"]]
        self.assertEqual(len(fetch_calls), 4)
        self.assertIn("retry 1/3 after git fetch failed", stdout.getvalue())
        self.assertIn("retry 3/3 after git fetch failed", stdout.getvalue())

    def test_git_clone_uses_hardcoded_retries(self) -> None:
        source = source_module.Source(
            name="remote-source",
            path=self.root / "checkout",
            remote=source_module.Remote(
                name="origin",
                url="https://example.invalid/acme/ExamplePack.git",
                ref="main",
            ),
        )
        calls: list[list[str]] = []

        def fail_run(cmd, *, cwd=None, dry_run: bool = False):
            calls.append(cmd)
            raise subprocess.CalledProcessError(128, cmd)

        stdout = io.StringIO()
        with (
            mock.patch.object(source_module, "run", side_effect=fail_run),
            mock.patch.object(source_module.time, "sleep"),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            source_module.sync_source(source, dry_run=False, depth=1)

        clone_calls = [cmd for cmd in calls if cmd[:2] == ["git", "clone"]]
        self.assertEqual(len(clone_calls), 4)
        self.assertIn("retry 1/3 after git clone failed", stdout.getvalue())

    def test_sync_source_existing_checkout_uses_depth_on_fetch(self) -> None:
        source_path = self.root / "checkout"
        source_path.mkdir()
        source = source_module.Source(
            name="remote-source",
            path=source_path,
            remote=source_module.Remote(
                name="origin",
                url="https://example.invalid/acme/ExamplePack.git",
                ref="main",
            ),
        )
        missing_remote = subprocess.CompletedProcess(["git"], 1, "", "")

        stdout = io.StringIO()
        with (
            mock.patch.object(source_module, "git_ok", return_value=True),
            mock.patch.object(source_module.subprocess, "run", return_value=missing_remote),
            contextlib.redirect_stdout(stdout),
        ):
            source_module.sync_source(source, dry_run=True, depth=1)

        self.assertIn("git fetch --depth 1 origin", stdout.getvalue())

    def test_profile_list_formats_profiles_sorted_by_name(self) -> None:
        (self.root / "profiles" / "alpha").mkdir()
        (self.root / "profiles" / "alpha" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "alpha"
                description = "Alpha profile."

                [skill.workspace]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("profile", "list")
        lines = stdout.strip().splitlines()
        self.assertEqual(lines[0], "name\tdescription\tskills")
        self.assertEqual(lines[1], "alpha\tAlpha profile.\tworkspace")
        self.assertEqual(lines[2], "content\t-\t-")
        stdout, _stderr = self.run_cli("profile", "ls")
        self.assertIn("alpha\tAlpha profile.\tworkspace", stdout)

    def test_profile_top_level_short_alias(self) -> None:
        stdout, _stderr = self.run_cli("p", "ls")

        self.assertIn("content\t-\t-", stdout)

    def test_profile_add_metadata_only_and_duplicate_rejected(self) -> None:
        stdout, _stderr = self.run_cli(
            "profile",
            "add",
            "research",
            "--description",
            "Research profile.",
        )

        self.assertIn("added profile: research", stdout)
        profile = self.read_profile("research")
        self.assertEqual(profile["name"], "research")
        self.assertEqual(profile["description"], "Research profile.")
        self.assertNotIn("skill", profile)

        _stdout, stderr = self.run_cli("profile", "add", "research", expected=1)
        self.assertIn("profile already exists: research", stderr)

    def test_profile_add_with_initial_skill_and_dry_run(self) -> None:
        self.write_skill(self.root / "local-source" / "external-two")

        stdout, _stderr = self.run_cli(
            "profile",
            "add",
            "example-pack",
            "-AS",
            "local-source",
            "-i",
            "nested",
            "-e",
            "external-two",
            "--dry-run",
        )
        self.assertIn("Would create profile:", stdout)
        self.assertIn("[skill.local-source]", stdout)
        self.assertIn('include = ["nested"]', stdout)
        self.assertFalse((self.root / "profiles" / "example-pack").exists())

        stdout, _stderr = self.run_cli("profile", "add", "example-pack", "-AS", "local-source")
        self.assertIn("added profile: example-pack", stdout)
        self.assertEqual(self.read_profile("example-pack")["skill"]["local-source"], {})

    def test_profile_add_skill_name_infers_source_and_include(self) -> None:
        stdout, _stderr = self.run_cli(
            "profile",
            "add",
            "example-pack",
            "-AS",
            "external-one",
            "--dry-run",
        )

        self.assertIn("[skill.local-source]", stdout)
        self.assertIn('include = ["external-one"]', stdout)

    def test_profile_add_skill_name_conflict_fails(self) -> None:
        self.write_skill(self.root / "other-source" / "nested" / "external-one")
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """

                    [source.other-source]
                    path = "other-source"
                    """
                )
            )

        _stdout, stderr = self.run_cli("profile", "add", "example-pack", "-AS", "external-one", expected=1)

        self.assertIn("skill name 'external-one' is ambiguous. Choose one:", stderr)
        self.assertIn("hagency profile add example-pack -AS local-source:nested/external-one", stderr)
        self.assertIn("hagency profile add example-pack -AS other-source:nested/external-one", stderr)

    def test_profile_update_add_skill_merges_and_dedupes(self) -> None:
        self.write_skill(self.root / "local-source" / "other")
        self.write_skill(self.root / "local-source" / "old")
        self.write_skill(self.root / "local-source" / "draft")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                exclude = ["old"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "-i",
            "nested",
            "other",
            "-e",
            "old",
            "draft",
        )

        self.assertIn("updated profile: content", stdout)
        skill = self.read_profile()["skill"]["local-source"]
        self.assertEqual(skill["include"], ["nested", "other"])
        self.assertEqual(skill["exclude"], ["old", "draft"])

    def test_profile_update_add_skill_name_infers_source_and_include(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text('name = "content"\n', encoding="utf-8")

        stdout, _stderr = self.run_cli("profile", "update", "content", "-AS", "external-one")

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["external-one"])

    def test_profile_update_add_source_selector_reference(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text('name = "content"\n', encoding="utf-8")

        stdout, _stderr = self.run_cli("profile", "update", "content", "-AS", "local-source:nested/external-one")

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["nested/external-one"])

    def test_profile_update_remove_source_selector_reference(self) -> None:
        self.write_skill(self.root / "local-source" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested/external-one", "external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("profile", "update", "content", "-RS", "local-source:nested/external-one")

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["external-two"])

    def test_profile_update_ambiguous_skill_name_suggests_source_selector_references(self) -> None:
        self.write_skill(self.root / "local-source" / "skills" / "write")
        self.write_skill(self.root / "local-source" / "plugins" / "waza" / "skills" / "write")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        before = profile_path.read_text(encoding="utf-8")

        _stdout, stderr = self.run_cli("profile", "update", "content", "-AS", "write", expected=1)

        self.assertIn("skill name 'write' is ambiguous. Choose one:", stderr)
        self.assertIn("hagency profile update content -AS local-source:plugins/waza/skills/write", stderr)
        self.assertIn("hagency profile update content -AS local-source:skills/write", stderr)
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

    def test_profile_update_include_ambiguous_selector_fails_before_write(self) -> None:
        self.write_skill(self.root / "local-source" / "skills" / "write")
        self.write_skill(self.root / "local-source" / "plugins" / "waza" / "skills" / "write")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        before = profile_path.read_text(encoding="utf-8")

        _stdout, stderr = self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "--include",
            "write",
            expected=1,
        )

        self.assertIn("skill selector 'write' for source local-source matched multiple candidates", stderr)
        self.assertIn("plugins/waza/skills/write", stderr)
        self.assertIn("skills/write", stderr)
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

    def test_profile_update_short_alias_with_profile_short_alias(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["old"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("p", "u", "content", "-AS", "local-source", "-i", "nested")

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["old", "nested"])

    def test_profile_update_existing_all_include_stays_all(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        self.run_cli("profile", "update", "content", "-AS", "local-source", "--include", "nested")
        self.assertNotIn("include", self.read_profile()["skill"]["local-source"])

        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["*"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.run_cli("profile", "update", "content", "-AS", "local-source", "--include", "nested")
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["*"])

    def test_profile_update_add_skill_replace(self) -> None:
        self.write_skill(self.root / "local-source" / "other")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested", "old"]
                exclude = ["draft"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "--include",
            "other",
            "--replace",
        )

        self.assertIn("updated profile: content", stdout)
        skill = self.read_profile()["skill"]["local-source"]
        self.assertEqual(skill, {"include": ["other"]})

    def test_profile_update_remove_skill_with_short_alias(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("profile", "update", "content", "-RS", "local-source")

        self.assertIn("updated profile: content", stdout)
        self.assertNotIn("skill", self.read_profile())

    def test_profile_update_remove_skill_name_from_include_list(self) -> None:
        self.write_skill(self.root / "local-source" / "nested" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["external-one", "external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("profile", "update", "content", "-RS", "external-one")

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["external-two"])

    def test_profile_update_remove_skill_name_from_full_source_adds_exclude(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("profile", "update", "content", "-RS", "external-one")

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(self.read_profile()["skill"]["local-source"]["exclude"], ["external-one"])

    def test_profile_update_unknown_source_rejected(self) -> None:
        _stdout, stderr = self.run_cli("profile", "update", "content", "-AS", "missing", expected=1)
        self.assertIn("unknown source or skill: missing", stderr)

    def test_profile_update_unknown_skill_mentions_unsynced_remote_sources(self) -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """

                    [source.remote-pack.remote]
                    url = "https://example.invalid/acme/RemotePack.git"
                    """
                )
            )

        _stdout, stderr = self.run_cli("profile", "update", "content", "-AS", "frontend-design", expected=1)

        self.assertIn("unknown source or skill: frontend-design", stderr)
        self.assertIn("hagency source sync remote-pack", stderr)

    def test_profile_update_include_requires_add_skill(self) -> None:
        _stdout, stderr = self.run_cli("profile", "update", "content", "--include", "nested", expected=1)
        self.assertIn("--include and --exclude require --add-skill", stderr)

    def test_profile_rejects_unsafe_names(self) -> None:
        _stdout, stderr = self.run_cli("profile", "add", "../bad", expected=1)
        self.assertIn("unsafe profile name", stderr)

    def test_profile_remove_deletes_directory_and_dry_run_does_not(self) -> None:
        (self.root / "profiles" / "scratch" / "notes").mkdir(parents=True)
        (self.root / "profiles" / "scratch" / "config.toml").write_text('name = "scratch"\n', encoding="utf-8")
        (self.root / "profiles" / "scratch" / "notes" / "README.md").write_text("keep", encoding="utf-8")

        stdout, _stderr = self.run_cli("profile", "remove", "scratch", "--dry-run")
        self.assertIn("Would remove profile directory:", stdout)
        self.assertTrue((self.root / "profiles" / "scratch").exists())

        stdout, _stderr = self.run_cli("profile", "rm", "scratch")
        self.assertIn("removed profile: scratch", stdout)
        self.assertFalse((self.root / "profiles" / "scratch").exists())

    def test_profile_init_discovers_workspace_and_source_skills(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.workspace]

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "init", "-p", str(target), "content")
        self.assertIn("local-one", stdout)
        self.assertIn("external-one", stdout)
        self.assertTrue((target / ".agents" / "skills" / "local-one").is_symlink())
        self.assertTrue((target / ".agents" / "skills" / "external-one").is_symlink())

    def test_profile_init_copy_mode_creates_independent_skill_directory(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "init", "-p", str(target), "content", "-cp")

        copied = target / ".agents" / "skills" / "external-one"
        source = self.root / "local-source" / "nested" / "external-one"
        self.assertIn("copy", stdout)
        self.assertTrue(copied.is_dir())
        self.assertFalse(copied.is_symlink())
        self.assertTrue((copied / "SKILL.md").exists())

        (copied / "local-note.md").write_text("target-only\n", encoding="utf-8")
        self.assertFalse((source / "local-note.md").exists())

    def test_profile_init_symlink_dry_run_uses_shell_neutral_output(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "init", "-p", str(target), "content", "--dry-run")

        self.assertIn("link", stdout)
        self.assertNotIn("ln -s", stdout)
        self.assertFalse((target / ".agents" / "skills" / "external-one").exists())

    def test_profile_init_link_mode_copy_dry_run_does_not_create_destination(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "init", "-p", str(target), "content", "--link-mode", "copy", "--dry-run")

        self.assertIn("copy", stdout)
        self.assertNotIn("ln -s", stdout)
        self.assertFalse((target / ".agents" / "skills" / "external-one").exists())

    def test_profile_init_copy_refuses_existing_destination(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"
        self.run_cli("profile", "init", "-p", str(target), "content", "-cp")
        copied = target / ".agents" / "skills" / "external-one"
        marker = copied / "local-note.md"
        marker.write_text("keep\n", encoding="utf-8")

        _stdout, stderr = self.run_cli("profile", "init", "-p", str(target), "content", "-cp", expected=1)

        self.assertIn("refusing to overwrite existing skill destination", stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_profile_init_copy_conflicts_with_explicit_symlink_mode(self) -> None:
        _stdout, stderr = self.run_cli(
            "profile",
            "init",
            "-p",
            str(self.root / "target"),
            "content",
            "-cp",
            "--link-mode",
            "symlink",
            expected=1,
        )

        self.assertIn("-cp cannot be combined with --link-mode symlink", stderr)

    def test_profile_init_copy_long_option_is_not_registered(self) -> None:
        _stdout, stderr = self.run_cli(
            "profile",
            "init",
            "-p",
            str(self.root / "target"),
            "content",
            "--copy",
            expected=2,
        )

        self.assertIn("unrecognized arguments: --copy", stderr)

    def test_profile_init_windows_symlink_error_mentions_administrator_mode(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with (
            mock.patch.object(profile_module, "is_windows_platform", return_value=True),
            mock.patch.object(profile_module.os, "symlink", side_effect=OSError("permission denied")),
        ):
            _stdout, stderr = self.run_cli("profile", "init", "-p", str(self.root / "target"), "content", expected=1)

        self.assertIn("could not create symlink", stderr)
        self.assertIn("PowerShell or Git Bash as Administrator", stderr)
        self.assertIn("-cp", stderr)

    def test_duplicate_discovered_skill_names_fail_with_prefix_guidance(self) -> None:
        self.write_skill(self.root / "local-source" / "other" / "external-one")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("profile", "init", "-p", str(self.root / "target"), "content", expected=1)
        self.assertIn("duplicate discovered skill name", stderr)
        self.assertIn("more specific path prefix", stderr)

    def test_skill_include_accepts_prefix_path(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "init", "-p", str(target), "content")
        self.assertIn("external-one", stdout)
        self.assertTrue((target / ".agents" / "skills" / "external-one").is_symlink())

    def test_skill_include_star_and_exclude(self) -> None:
        self.write_skill(self.root / "local-source" / "nested" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["*"]
                exclude = ["external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "init", "-p", str(target), "content")
        self.assertIn("external-one", stdout)
        self.assertNotIn("external-two", stdout)
        self.assertTrue((target / ".agents" / "skills" / "external-one").is_symlink())
        self.assertFalse((target / ".agents" / "skills" / "external-two").exists())

    def test_skill_list_default_scans_workspace_and_sources(self) -> None:
        stdout, stderr = self.run_cli("skill", "list")
        lines = stdout.strip().splitlines()

        self.assertEqual(lines[0], "source\tname\tselector\tpath")
        self.assertIn(
            f"workspace\tlocal-one\tskills/local-one\t{(self.root / 'skills' / 'local-one').resolve()}",
            lines,
        )
        self.assertIn(
            f"local-source\texternal-one\tnested/external-one\t{(self.root / 'local-source' / 'nested' / 'external-one').resolve()}",
            lines,
        )
        self.assertEqual(stderr, "")

        stdout, _stderr = self.run_cli("skill", "ls")
        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)

    def test_skill_list_source_filters(self) -> None:
        stdout, _stderr = self.run_cli("skill", "list", "--source", "workspace")

        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)
        self.assertNotIn("local-source\texternal-one", stdout)

        stdout, _stderr = self.run_cli("skill", "list", "-s", "local-source")

        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertNotIn("workspace\tlocal-one", stdout)

    def test_skill_list_multiple_source_filters_preserve_order_and_dedupe(self) -> None:
        stdout, _stderr = self.run_cli(
            "skill",
            "list",
            "-s",
            "local-source",
            "-s",
            "workspace",
            "-s",
            "local-source",
        )
        lines = stdout.strip().splitlines()

        self.assertEqual(lines[0], "source\tname\tselector\tpath")
        self.assertEqual([line.split("\t")[0] for line in lines[1:]], ["local-source", "workspace"])

    def test_skill_list_profile_applies_include_and_exclude(self) -> None:
        self.write_skill(self.root / "local-source" / "nested" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.workspace]
                include = ["skills/local-one"]

                [skill.local-source]
                include = ["*"]
                exclude = ["external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("skill", "list", "--profile", "content")

        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)
        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertNotIn("external-two", stdout)

    def test_skill_list_profile_and_source_filter_intersect(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.workspace]
                include = ["skills/local-one"]

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("skill", "list", "-p", "content", "-s", "local-source")

        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertNotIn("workspace\tlocal-one", stdout)

    def test_skill_list_profile_can_show_duplicate_star_matches(self) -> None:
        self.write_skill(self.root / "local-source" / "other" / "external-one")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("skill", "list", "-p", "content")

        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertIn("local-source\texternal-one\tother/external-one", stdout)

    def test_skill_list_default_skips_missing_sources_with_warning(self) -> None:
        self.append_remote_source("remote-source")

        stdout, stderr = self.run_cli("skill", "list")

        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)
        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertIn("Warning: skipping missing source remote-source:", stderr)

    def test_skill_list_rejects_unknown_missing_source_and_unknown_profile(self) -> None:
        _stdout, stderr = self.run_cli("skill", "list", "-s", "missing", expected=1)
        self.assertIn("unknown source: missing", stderr)

        self.append_remote_source("remote-source")
        _stdout, stderr = self.run_cli("skill", "list", "-s", "remote-source", expected=1)
        self.assertIn("source path does not exist; run hagency source sync first", stderr)

        _stdout, stderr = self.run_cli("skill", "list", "-p", "missing", expected=1)
        self.assertIn("missing config:", stderr)

    def test_legacy_sources_schema_is_rejected(self) -> None:
        self.config_path.write_text(
            textwrap.dedent(
                """
                [[sources]]
                name = "old"
                path = "old"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("source", "list", expected=1)
        self.assertIn("legacy [[sources]]", stderr)

    def test_legacy_skills_schema_is_rejected(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [[skills]]
                source = "local-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("profile", "init", "-p", str(self.root / "target"), "content", expected=1)
        self.assertIn("legacy [[skills]]", stderr)

    def test_profile_skill_subcommand_is_not_registered(self) -> None:
        _stdout, stderr = self.run_cli("profile", "skill", expected=2)
        self.assertIn("invalid choice", stderr)


if __name__ == "__main__":
    unittest.main()
