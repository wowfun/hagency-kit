import contextlib
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from hagency_cli.cli import main
from hagency_cli.workspace import (
    ffmpeg_portable,
    tool_native,
    tool_packages,
    tool_state,
)
from hagency_cli.workspace.config import write_toml
from hagency_cli.workspace.errors import WorkspaceError
from hagency_cli.workspace.operations import tools
from hagency_cli.workspace.tool_catalog import Status, effective_tools
from hagency_cli.workspace.tool_process import process


class ToolLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.public = self.root / "installed"
        self.enterContext(
            patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        )
        self.registry = self.root / "hagency-config.toml"
        self.declare({"demo": {"install": "uv"}})

    def declare(self, entries):
        registry = {"tool": entries}
        write_toml(self.registry, registry)
        return effective_tools(registry, {})

    def cli(self, *args, code=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                main([*args, "--root", str(self.root)])
                actual = 0
            except SystemExit as exc:
                actual = exc.code
        self.assertEqual(actual, code, output.getvalue())
        return output.getvalue()

    def records(self, name):
        with tool_state.receipts(False) as db:
            return tool_state.lookup(db, name)

    def wheel(self, version):
        name = "hgc_review_fixture"
        file = self.root / f"{name}-{version}-py3-none-any.whl"
        prefix = f"{name}-{version}.dist-info/"
        with zipfile.ZipFile(file, "w") as wheel:
            wheel.writestr(name + ".py", f"def main():\n    print({version!r})\n")
            wheel.writestr(
                prefix + "METADATA",
                f"Metadata-Version: 2.1\nName: hgc-review-fixture\nVersion: {version}\n",
            )
            wheel.writestr(
                prefix + "WHEEL",
                "Wheel-Version: 1.0\nGenerator: hgc-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(
                prefix + "entry_points.txt",
                f"[console_scripts]\nhgc-review-fixture = {name}:main\n",
            )
            files = [*wheel.namelist(), prefix + "RECORD"]
            wheel.writestr(files[-1], "".join(f"{item},,\n" for item in files))

    @unittest.skipUnless(shutil.which("uv"), "real uv tool installation")
    def test_offline_uv_cli_install_upgrade_and_uninstall(self):
        name = "hgc-review-fixture"
        self.declare({name: {"install": "uv"}})
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "UV_NO_CONFIG": "1",
                    "UV_OFFLINE": "1",
                    "UV_NO_INDEX": "1",
                    "UV_FIND_LINKS": str(self.root),
                    "UV_CACHE_DIR": str(self.root / "cache"),
                    "UV_PYTHON": sys.executable,
                    "UV_PYTHON_DOWNLOADS": "never",
                    "PATH": os.pathsep.join(
                        [str(self.public / "bin"), os.environ["PATH"]]
                    ),
                },
            )
        )
        expose = self.enterContext(
            patch.object(tool_state, "expose_windows", return_value=True)
        )
        self.wheel("1.0")
        if os.name == "nt":
            expose.side_effect = [OSError("registry unavailable"), True, False, True]
            self.assertIn(
                "registry unavailable",
                self.cli("tool", "i", name, "--install-dir", str(self.public), code=1),
            )
            self.assertEqual(len(self.records(name)), 1)
        self.cli("tool", "i", name, "--install-dir", str(self.public))
        (record,) = self.records(name)
        self.assertTrue(Path(record.path).is_file())
        self.assertIn("\tavailable\t1.0\t", self.cli("tool", "check", name))
        if os.name == "nt":
            with tool_state.receipts(False) as db:
                self.assertEqual(
                    db.execute("SELECT path FROM published_dirs").fetchall(),
                    [(str(Path(record.path).parent),)],
                )
        self.wheel("2.0")
        self.cli("tool", "u", name)
        self.assertIn("\tavailable\t2.0\t", self.cli("tool", "check", name))
        self.cli("tool", "uninstall", name)
        self.assertFalse(Path(record.path).exists())
        self.assertEqual(self.records(name), [])
        self.assertIn("\tmissing\t", self.cli("tool", "check", name, code=1))
        if os.name == "nt":
            expose.assert_called_with(Path(record.path).parent, remove=True)
            with tool_state.receipts(False) as db:
                self.assertEqual(
                    db.execute("SELECT path FROM published_dirs").fetchall(), []
                )

    def test_forget_after_catalog_removal_selects_one_receipt_and_leaves_files(self):
        dirs = [self.public, self.root / "second"]
        with tool_state.receipts(True) as db:
            for root in dirs:
                root.mkdir()
                binary = root / "demo.exe"
                binary.write_bytes(b"manually replaced")
                tool_state.remember(
                    db,
                    tool_state.Receipt(
                        "demo", str(root), "uv", "demo", str(binary), None
                    ),
                )
                db.execute("INSERT INTO published_dirs (path) VALUES (?)", (str(root),))
        self.cli("tool", "rm", "demo")
        probe = self.enterContext(patch.object(tools, "probe"))
        expose = self.enterContext(patch.object(tool_state, "expose_windows"))
        self.assertIn(
            "multiple recorded",
            self.cli("tool", "uninstall", "demo", "--forget", code=1),
        )
        command = [
            "tool",
            "uninstall",
            "demo",
            "--forget",
            "--install-dir",
            str(dirs[0]),
        ]
        self.assertIn("Would forget", self.cli(*command, "--dry-run"))
        self.assertEqual(len(self.records("demo")), 2)
        self.assertIn("Forgot", self.cli(*command))
        self.assertEqual([r.root for r in self.records("demo")], [str(dirs[1])])
        self.assertEqual((dirs[0] / "demo.exe").read_bytes(), b"manually replaced")
        probe.assert_not_called()
        expose.assert_not_called()

    def test_installer_leaves_database_writable_and_excludes_competing_package(self):
        tool = self.declare({"demo": {"install": "uv"}})["demo"]
        with tool_state.receipts(True) as db:
            file = db.execute("PRAGMA database_list").fetchone()[2]

        def installer(*args, **kwargs):
            with contextlib.closing(sqlite3.connect(file, timeout=0)) as other:
                other.execute("BEGIN IMMEDIATE")
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from hagency_cli.workspace.tool_state import tool_lock\n"
                        "from hagency_cli.workspace.errors import WorkspaceError\n"
                        "with tool_lock('independent-tool'): pass\n"
                        "try:\n with tool_lock('demo'): pass\n"
                        "except WorkspaceError as e:\n print(e)\n"
                        "else:\n raise AssertionError('overlapping install allowed')"
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("another tool operation", child.stdout)
            raise WorkspaceError("simulated installer failure")

        with (
            patch.object(tool_packages, "change_package", side_effect=installer),
            patch.object(tools.shutil, "which", return_value="uv"),
            self.assertRaisesRegex(WorkspaceError, "simulated installer failure"),
        ):
            tools.manage_tool(tool, "install", install_dir=str(self.public))
        self.assertEqual(self.records("demo"), [])
        with tool_state.tool_lock("demo"):
            pass

    def test_invalid_metadata_has_source_and_empty_results_are_valid(self):
        tool = self.declare({"demo": {"install": "uv"}})["demo"]
        receipt = self.public / "uv/demo/uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        for content in ("[other]\n", '[tool]\nentrypoints = [{name = "demo"}]'):
            receipt.write_text(content)
            with self.assertRaisesRegex(WorkspaceError, "invalid metadata from"):
                tool_packages.owned_entry(tool, "uv", self.public)
        with patch.object(tool_native, "query_json", return_value={"casks": []}):
            self.assertEqual(tool_native.candidates("brew", "demo"), [])
        with (
            patch.object(
                tool_native, "query_json", return_value={"formulae": [{"name": "demo"}]}
            ),
            self.assertRaisesRegex(WorkspaceError, "brew info for demo"),
        ):
            tool_native.candidates("brew", "demo")
        with patch.object(tool_packages, "query_json", return_value=[]):
            self.assertIsNone(tool_packages.owned_entry(tool, "pnpm", None))

    def test_pnpm_fallback_uses_the_row_that_owns_the_package(self):
        tool = self.declare({"demo": {"install": "pnpm"}})["demo"]
        directory = self.public / "node_modules/demo"
        directory.mkdir(parents=True)
        (directory / "package.json").write_text('{"name":"demo","bin":"run.js"}')
        target = directory / "run.js"
        target.touch()
        shim = self.root / ("demo.cmd" if os.name == "nt" else "demo")
        os.link(target, shim)
        rows = [
            {"path": str(self.root / "wrong")},
            {"path": str(self.public), "dependencies": {"demo": {}}},
        ]
        with (
            patch.object(tool_packages, "query_json", return_value=rows),
            patch.object(tool_packages, "process") as run,
        ):
            run.return_value.stdout = str(self.root)
            self.assertEqual(tool_packages.owned_entry(tool, "pnpm", None), shim)

    def test_ambiguous_winget_registration_preserves_external_installation(self):
        tool = self.declare({"demo": {"install": "winget"}})["demo"]
        for child in ("old", "new"):
            directory = self.public / child
            directory.mkdir(parents=True)
            (directory / "demo.exe").touch()
        with (
            patch.object(tool_native, "winget_location", return_value=self.public),
            patch.object(tools.shutil, "which", return_value="winget"),
            patch.object(tool_native, "change_native") as change,
        ):
            with self.assertRaisesRegex(WorkspaceError, "unique demo.exe"):
                tools.manage_tool(
                    tool, "install", install_dir=str(self.root / "separate")
                )
            change.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows PATH ownership")
    def test_unpublish_only_after_last_record_and_command_are_gone(self):
        self.public.mkdir()
        directory = str(self.public)
        expose = self.enterContext(
            patch.object(tool_state, "expose_windows", return_value=True)
        )
        records = [
            tool_state.Receipt(
                name, directory, "uv", name, str(self.public / (name + ".exe")), None
            )
            for name in ("first", "last")
        ]
        with tool_state.receipts(True) as db:
            for record in records:
                tool_state.remember(db, record)
            db.execute("INSERT INTO published_dirs (path) VALUES (?)", (directory,))
            tool_state.release_receipt(db, records[0])
            expose.assert_not_called()
            unrelated = self.public / "unrelated.cmd"
            unrelated.touch()
            tool_state.release_receipt(db, records[1])
            expose.assert_not_called()
            unrelated.unlink()
            tool_state.remember(db, records[1])
            tool_state.release_receipt(db, records[1])
            expose.assert_called_once_with(self.public, remove=True)

    def test_native_noop_uses_one_discovery_and_probe(self):
        tool = self.declare({"demo": {"install": "brew"}})["demo"]
        path = Path(sys.executable)
        with (
            patch.object(tools, "active_path", return_value=path),
            patch.object(tools.shutil, "which", return_value="brew"),
            patch.object(tool_native, "owned_entry", return_value=path) as owner,
            patch.object(tool_native, "compatible", return_value="1.2"),
            patch.object(
                tools,
                "probe",
                return_value=Status(path, tools.upstream_version("1.2"), "available"),
            ) as probe,
            patch.object(tool_native, "change_native") as change,
        ):
            events = []
            tools.manage_tool(tool, "upgrade", progress=events.append)
            self.assertEqual(len(events), 1)
            owner.assert_called_once()
            probe.assert_called_once()
            change.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "native Windows lacks POSIX UID")
    def test_msys_pacman_has_no_posix_privilege_check(self):
        tool = self.declare({"demo": {"install": "pacman"}})["demo"]
        with patch.object(tool_native, "process") as run:
            tool_native.change_native(tool, "pacman", None, "install", "1.0", ())
            self.assertEqual(
                run.call_args.args[0], ["pacman", "-S", "--noconfirm", "demo"]
            )

    def test_portable_rejects_corrupt_download_and_changed_destination(self):
        tool = self.declare({"ffmpeg": {}})["ffmpeg"]
        path = ffmpeg_portable.destination(self.public)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as bundle:
            bundle.writestr("bin/" + path.name, b"verified binary")
        content = data.getvalue()
        with (
            patch.object(ffmpeg_portable, "release") as release,
            patch.object(
                ffmpeg_portable, "fetch", side_effect=lambda _: io.BytesIO(content)
            ),
            patch.object(
                ffmpeg_portable, "probe", return_value=Status(path, None, "available")
            ),
        ):
            release.return_value = ("test-url", "0" * 64)
            with self.assertRaisesRegex(WorkspaceError, "checksum mismatch"):
                ffmpeg_portable.change_portable(tool, path, None, False)
            self.assertFalse(path.exists())
            release.return_value = ("test-url", hashlib.sha256(content).hexdigest())
            checksum = ffmpeg_portable.change_portable(tool, path, None, False)
            self.assertEqual(path.read_bytes(), b"verified binary")
            with tool_state.receipts(True) as db:
                tool_state.remember(
                    db,
                    tool_state.Receipt(
                        "ffmpeg",
                        str(self.public),
                        "portable",
                        "ffmpeg",
                        str(path),
                        checksum,
                    ),
                )
            path.write_bytes(b"replaced")
            with self.assertRaisesRegex(WorkspaceError, "unrecognized file"):
                tools.manage_tool(tool, "uninstall", install_dir=str(self.public))
            self.assertEqual(path.read_bytes(), b"replaced")
            self.assertEqual(len(self.records("ffmpeg")), 1)

    def test_installer_progress_is_visible_before_exit_and_retains_errors(self):
        signal = self.root / "continue"
        code = (
            "import pathlib, sys, time\np=pathlib.Path(sys.argv[1])\n"
            "print('waiting', flush=True)\nwhile not p.exists(): time.sleep(0.01)\n"
            "print('diagnostic', file=sys.stderr)\nsys.exit(3)"
        )

        def receive(event):
            if event.message == "waiting":
                signal.touch()

        with self.assertRaisesRegex(WorkspaceError, r"(?s)exited 3:.*diagnostic"):
            process(
                [sys.executable, "-c", code, str(signal)], progress=receive, timeout=10
            )
        with self.assertRaisesRegex(WorkspaceError, "timed out"):
            process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                progress=receive,
                timeout=0.2,
            )

    def test_profile_continues_after_one_tool_fails(self):
        self.declare({name: {"install": "uv"} for name in ("first", "second", "last")})
        profile = self.root / "profiles/dev/config.toml"
        profile.parent.mkdir(parents=True)
        write_toml(profile, {"name": "dev"})
        with patch.object(
            tools,
            "manage_tool",
            side_effect=[WorkspaceError("denied"), None, WorkspaceError("offline")],
        ) as manage:
            out = self.cli(
                "p", "i", "dev", "-T", "--dry-run", "--dir", str(self.root), code=1
            )
        self.assertEqual(
            [call.args[0].name for call in manage.call_args_list],
            ["first", "second", "last"],
        )
        self.assertIn("first: denied", out)
        self.assertIn("last: offline", out)
