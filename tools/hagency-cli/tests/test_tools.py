import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.version import Version

from hagency_cli.cli import main
from hagency_cli.workspace import tool_native, tool_packages
from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import WorkspaceError
from hagency_cli.workspace.operations.tools import manage_tool
from hagency_cli.workspace.tool_catalog import Status, catalog, effective_tools, probe


class ToolTests(unittest.TestCase):
    def test_native_package_install_uses_the_terminal_for_sudo_authentication(self):
        tool = effective_tools({"tool": {"demo": {}}}, {})["demo"]
        with (
            patch.object(os, "geteuid", return_value=1000, create=True),
            patch.object(tool_native.shutil, "which", return_value="/usr/bin/sudo"),
            patch.object(tool_native, "process") as run,
            patch.object(sys.stdin, "isatty") as tty,
        ):
            for interactive in (True, False):
                tty.return_value = interactive
                tool_native.change_native(tool, "apt", None, "install", "2.0", ())
                command = run.call_args.args[0]
                self.assertEqual(command[0], "sudo")
                self.assertEqual("-n" in command, not interactive)
                self.assertIn("demo=2.0", command)

    def test_uv_receipt_requires_all_configured_extras(self):
        tool = effective_tools(
            {"tool": {"demo": {"package": "demo[pdf,docx,pptx]", "install": "uv"}}}, {}
        )["demo"]
        path = self.root / "demo/uv-receipt.toml"
        path.parent.mkdir()
        with patch.object(tool_packages, "process") as run:
            run.return_value.stdout = str(self.root)
            for extras, expected in ((["pdf"], False), (["pptx", "pdf", "docx"], True)):
                path.write_text(
                    '[tool]\nrequirements = [{ name = "demo", extras = '
                    + json.dumps(extras)
                    + " }]"
                )
                self.assertEqual(tool_packages.uv_has_extras(tool, None), expected)

    def test_reported_versions_keep_prerelease_and_postrelease_qualifiers(self):
        from hagency_cli.workspace.tool_catalog import upstream_version

        for banner, expected in (
            ("demo version 1.2.dev3", "1.2.dev3"),
            ("demo 1.2.post1", "1.2.post1"),
            ("demo version 2", "2"),
            ("ffmpeg version 7:6.1.1-3ubuntu5", "6.1.1"),
        ):
            with self.subTest(banner=banner):
                self.assertEqual(upstream_version(banner), Version(expected))

    def test_failed_command_is_unavailable_without_a_version_constraint(self):
        tool = effective_tools(
            {
                "tool": {
                    "demo": {"version_args": ["-c", "raise RuntimeError('broken')"]}
                }
            },
            {},
        )["demo"]
        self.assertEqual(probe(tool, Path(sys.executable)).status, "unavailable")

    @unittest.skipUnless(os.name == "nt", "Windows user PATH and installation roots")
    def test_windows_path_registration_and_unavailable_drive(self):
        import uuid
        import winreg

        from hagency_cli.workspace.tool_state import expose_windows, windows_root

        base = "Software\\HagencyTest-" + uuid.uuid4().hex
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as sandbox:
            self.addCleanup(winreg.DeleteKey, winreg.HKEY_CURRENT_USER, base)
            self.addCleanup(
                winreg.DeleteKey, winreg.HKEY_CURRENT_USER, base + "\\Environment"
            )
            with patch.object(winreg, "HKEY_CURRENT_USER", sandbox):
                expose_windows(self.root)
                expose_windows(self.root)
                with winreg.OpenKey(sandbox, "Environment") as key:
                    self.assertEqual(
                        winreg.QueryValueEx(key, "Path")[0], str(self.root)
                    )
        with (
            patch.object(Path, "is_dir", return_value=False),
            patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}),
        ):
            self.assertEqual(windows_root(True), self.root / "hagency/tools")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.registry = self.root / "hagency-config.toml"
        self.profile = self.root / "profiles/dev/config.toml"
        self.profile.parent.mkdir(parents=True)
        write_toml(
            self.registry,
            {"tool": {"demo": {"install": ["npm"], "install_args": ["--flag", "a b"]}}},
        )
        write_toml(self.profile, {"name": "dev"})
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args, code=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                main([*args, "--root", str(self.root)])
                actual = 0
            except SystemExit as exc:
                actual = exc.code
        self.assertEqual(actual, code, buf.getvalue())
        return buf.getvalue()

    def test_aliases_config_edits_and_inheritance_survive_other_writers(self):
        self.cli("tool", "a", "demo", "--version", ">=1,<3", "--profile", "dev")
        tool = catalog(self.root, "dev")["demo"]
        self.assertEqual(tool.methods, ("npm",))
        self.assertEqual(tool.install_args, ("--flag", "a b"))
        self.assertTrue(tool.accepts(Version("1.0")))
        self.assertFalse(tool.accepts(Version("3")))
        self.cli("p", "u", "dev", "--description", "edited")
        self.cli("source", "a", "here", "--path", str(self.root))
        self.assertEqual(catalog(self.root, "dev")["demo"], tool)
        self.cli("tool", "rm", "demo", "--profile", "dev")
        self.assertEqual(catalog(self.root, "dev"), {})
        self.assertIn("demo", catalog(self.root, None))
        self.cli("tool", "a", "demo", "--profile", "dev")
        self.assertIn("demo", catalog(self.root, "dev"))
        self.cli("tool", "a", "extra", "--profile", "dev", "--install", "uv")
        self.cli("tool", "rm", "extra", "--profile", "dev")
        self.assertNotIn("extra", read_toml(self.profile)["tool"])

    def test_explicit_profile_tools_and_dry_run(self):
        with patch("hagency_cli.workspace.operations.tools.manage_tool") as manage:
            for action in ("apply", "install", "i"):
                self.cli("p", action, "dev", "--dry-run", "--dir", str(self.root))
            manage.assert_not_called()
            self.cli("p", "i", "dev", "-T", "--dry-run", "--install-dir", "my tools")
            self.assertEqual(manage.call_count, 1)
            self.assertEqual(manage.call_args.args[0].name, "demo")
            self.assertTrue(manage.call_args.kwargs["dry_run"])
            self.assertEqual(manage.call_args.kwargs["install_dir"], "my tools")
        self.cli("p", "a", "profile-created")
        self.assertTrue(
            (self.profile.parents[1] / "profile-created/config.toml").is_file()
        )

    def test_passthrough_and_upgrade_aliases(self):
        with patch("hagency_cli.commands.tool.manage_tool") as manage:
            for action in ("install", "i", "upgrade", "update", "u"):
                try:
                    main(
                        [
                            "tool",
                            action,
                            "demo",
                            "--root",
                            str(self.root),
                            "--",
                            "--index",
                            "two words",
                            "--foo=bar",
                        ]
                    )
                except SystemExit as e:
                    self.assertEqual(e.code, 0)
                self.assertEqual(
                    manage.call_args.args[1],
                    "install" if action in {"i", "install"} else "upgrade",
                )
                self.assertEqual(
                    manage.call_args.kwargs["extra"],
                    ("--index", "two words", "--foo=bar"),
                )

    def test_list_reports_configured_and_detected_versions_and_strict_exits(self):
        command = Path(sys.executable).stem
        write_toml(
            self.registry,
            {
                "tool": {
                    "hgc-missing-tool-test": {"install": "uv"},
                    "demo": {
                        "command": command,
                        "version": ">=1,<2",
                        "version_args": ["-c", "print('1.4.0')"],
                        "install": ["pnpm", "npm"],
                        "package": "demo-cli",
                    },
                }
            },
        )
        missing = "Undetected: hgc-missing-tool-test\n    install: uv\n"
        available = (
            "Detected: demo v1.4.0\n"
            f"    command: {command}\n"
            f"    path: {Path(shutil.which(command)).absolute()}\n"
            "    install: pnpm, npm\n"
            "    package: demo-cli\n"
        )
        expected = missing + "\n" + available
        self.assertEqual(self.cli("tool", "ls"), expected)
        self.assertEqual(self.cli("tool", "list"), expected)
        self.assertEqual(self.cli("tool", "ls", "--strict", code=1), expected)
        with patch("hagency_cli.commands.tool.probe", wraps=probe) as detect:
            self.assertEqual(self.cli("tool", "ls", "demo", "--strict"), available)
            detect.assert_called_once()

    def test_list_profile_uses_effective_constraints_and_enabled_tools(self):
        write_toml(
            self.registry,
            {
                "tool": {
                    "demo": {
                        "command": Path(sys.executable).stem,
                        "version_args": ["-c", "print('2.0')"],
                    },
                    "disabled": {},
                }
            },
        )
        for version_args, required, heading, code in (
            (["-c", "print('2.0')"], "<2", "Detected: demo v2.0", 1),
            (["-c", "print('unknown')"], "<2", "Detected: demo", 1),
            (["-c", "print('unknown')"], "*", "Detected: demo", 0),
            (["-c", "raise SystemExit(1)"], "<2", "Undetected: demo", 1),
        ):
            with self.subTest(args=version_args, required=required):
                write_toml(
                    self.profile,
                    {
                        "name": "dev",
                        "tool": {
                            "disabled": {"enabled": False},
                            "demo": {"version": required, "version_args": version_args},
                        },
                    },
                )
                output = self.cli(
                    "tool", "ls", "--profile", "dev", "--strict", code=code
                )
                self.assertNotIn("disabled", output)
                self.assertEqual(output.splitlines()[0], heading)
                if code:
                    self.assertIn(f"    configured_version: {required}\n", output)
                else:
                    self.assertNotIn("configured_version:", output)

    def test_list_omits_all_redundant_fields_even_installer(self):
        write_toml(self.registry, {"tool": {"uv": {"install": "uv", "version": "*"}}})
        with patch.dict(os.environ, {"PATH": str(self.root)}):
            self.assertEqual(self.cli("tool", "ls", "uv"), "Undetected: uv\n")

    def test_list_no_probe_shows_effective_configuration_without_running_tools(self):
        write_toml(
            self.profile,
            {"tool": {"demo": {"version": "<2"}, "extra": {"install": "uv"}}},
        )
        with patch(
            "hagency_cli.commands.tool.probe", side_effect=AssertionError("unexpected probe")
        ):
            self.assertEqual(
                self.cli("tool", "list", "--no-probe"), "demo\n    install: npm\n"
            )
            self.assertEqual(
                self.cli("tool", "ls", "--no-probe", "--profile", "dev"),
                "demo\n    configured_version: <2\n    install: npm\n\nextra\n    install: uv\n",
            )
            self.assertEqual(
                self.cli("tool", "ls", "extra", "--no-probe", "--profile", "dev"),
                "extra\n    install: uv\n",
            )
            self.assertIn(
                "not in the effective catalog", self.cli("tool", "ls", "extra", "--no-probe", code=1)
            )
            self.assertIn(
                "mutually exclusive", self.cli("tool", "ls", "--no-probe", "--strict", code=2)
            )

    def test_list_unknown_probe_status_does_not_claim_detection(self):
        with patch(
            "hagency_cli.commands.tool.probe", return_value=Status(None, None, "new-failure")
        ):
            self.assertEqual(
                self.cli("tool", "ls", "--strict", code=1), "Undetected: demo\n    install: npm\n"
            )

    def test_list_empty_catalog_errors_and_removed_check_command(self):
        write_toml(self.registry, {})
        self.assertEqual(self.cli("tool", "list", "--strict"), "")
        self.assertIn(
            "not in the effective catalog", self.cli("tool", "ls", "other", code=1)
        )
        write_toml(self.registry, {"tool": {"demo": {"version": "invalid"}}})
        self.assertIn("invalid tool version range", self.cli("tool", "ls", code=1))
        self.assertIn("No such command", self.cli("tool", "check", code=2))

    def test_unrecognized_external_executable_is_never_uninstalled(self):
        tool = effective_tools(
            {
                "tool": {
                    "demo": {"command": Path(sys.executable).stem, "install": ["uv"]}
                }
            },
            {},
        )["demo"]
        with (
            patch("hagency_cli.workspace.operations.tools.owned", return_value=None),
            patch.object(tool_packages, "change_package") as change,
        ):
            with self.assertRaisesRegex(WorkspaceError, "no installation owned"):
                manage_tool(tool, "uninstall")
            change.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX pnpm command shim")
    def test_pnpm_quoted_shell_shim_ownership(self):
        directory = self.root / "package dir"
        directory.mkdir()
        (directory / "package.json").write_text(
            json.dumps({"name": "demo", "bin": "cli.js"})
        )
        (directory / "cli.js").touch()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "demo"
        shim.write_text(
            """#!/bin/sh\nexec node "$basedir_abs"/'../package dir/cli.js' "$@"\n"""
        )
        tool = catalog(self.root, None)["demo"]
        with (
            patch.object(
                tool_packages,
                "query_json",
                return_value=[{"dependencies": {"demo": {"path": str(directory)}}}],
            ),
            patch.object(tool_packages, "process") as process,
        ):
            process.return_value.stdout = str(bin_dir)
            self.assertEqual(tool_packages.owned_entry(tool, "pnpm", None), shim)
            shim.write_text("#!/bin/sh\nexec unrelated\n")
            self.assertIsNone(tool_packages.owned_entry(tool, "pnpm", None))
