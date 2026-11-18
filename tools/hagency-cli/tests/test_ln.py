from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from hagency_cli.cli import app
from hagency_cli.workspace.skills import is_junction
from tests.support import directory_link, file_symlink


class LinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(contextlib.chdir(self.root))

    def run_ln(self, *args: str, expected: int = 0):
        result = CliRunner().invoke(
            app, ["ln", *args], prog_name="hgc", catch_exceptions=False
        )
        self.assertEqual(result.exit_code, expected, result.output)
        return result

    def test_help_and_usage(self) -> None:
        help_text = self.run_ln("-h").stdout
        self.assertIn("Usage: hgc ln [OPTIONS] {TARGET} [DEST]", help_text)
        self.assertIn("-s, --symbolic", help_text)
        self.assertIn("junction", help_text)
        for args in [("assets", "link"), ("-s",), ("-s", "a", "b", "c")]:
            with self.subTest(args=args):
                self.run_ln(*args, expected=2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_directory_link_with_explicit_name(self) -> None:
        target = self.root / "目标 [x] & it's"
        target.mkdir()
        (target / "contents").write_text("directory", encoding="utf-8")
        link = self.root / "链接 [y]"
        result = self.run_ln("--symbolic", target.name, link.name)
        self.assertEqual(result.output, "")
        self.assertEqual((link / "contents").read_text(encoding="utf-8"), "directory")
        self.assertEqual(link.resolve(), target)
        if os.name == "nt":
            self.assertTrue(is_junction(link))
        else:
            self.assertEqual(os.readlink(link), target.name)

    def test_omitting_destination_creates_link_in_current_directory(self) -> None:
        target = self.root / "shared" / "assets"
        target.mkdir(parents=True)

        self.run_ln("-s", "shared/assets")

        link = self.root / "assets"
        self.assertEqual(link.resolve(), target)
        if os.name == "nt":
            self.assertTrue(is_junction(link))
        else:
            self.assertEqual(os.readlink(link), "shared/assets")

    def test_existing_destination_directory_uses_link_parent(self) -> None:
        assets = self.root / "site" / "assets"
        assets.mkdir(parents=True)
        (assets / "root-only").touch()
        actual = assets.parent / "actual"
        actual.mkdir()
        public = self.root / "public"
        directory_link(public, actual)

        self.run_ln("-s", "../assets", "public")

        link = actual / "assets"
        self.assertTrue((link / "root-only").is_file())
        if os.name == "nt":
            self.assertTrue(is_junction(link))
        else:
            self.assertEqual(os.readlink(link), "../assets")

    def test_file_link(self) -> None:
        target = self.root / "original.txt"
        target.write_text("contents", encoding="utf-8")
        link = self.root / "copy.txt"
        result = CliRunner().invoke(
            app,
            ["ln", "-s", target.name, link.name],
            prog_name="hgc",
            catch_exceptions=False,
        )
        if os.name == "nt" and result.exit_code == 1:
            self.assertIn("Developer Mode", result.stderr)
            self.assertFalse(os.path.lexists(link))
        else:
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.read_text(encoding="utf-8"), "contents")

    def test_missing_target(self) -> None:
        link = self.root / "link"
        if os.name == "nt":
            result = self.run_ln("-s", "future", link.name, expected=1)
            self.assertIn("target does not exist", result.stderr)
            self.assertFalse(os.path.lexists(link))
        else:
            self.run_ln("-s", "./future", link.name)
            self.assertEqual(os.readlink(link), "./future")
            self.assertFalse(link.exists())

    def test_missing_parent_is_not_created(self) -> None:
        (self.root / "assets").mkdir()
        result = self.run_ln("-s", "assets", "missing/link", expected=1)
        self.assertIn("parent", result.stderr)
        self.assertFalse((self.root / "missing").exists())

    def test_existing_entries_are_preserved(self) -> None:
        target = self.root / "assets"
        target.mkdir()
        dest = self.root / "public"
        dest.mkdir()
        link = dest / "assets"
        link.mkdir()
        self.run_ln("-s", "../assets", "public", expected=1)
        self.assertTrue(link.is_dir())
        self.assertFalse(is_junction(link) or link.is_symlink())
        link.rmdir()

        link.touch()
        self.run_ln("-s", "../assets", "public", expected=1)
        self.assertTrue(link.is_file())

    def test_dangling_destination_is_preserved(self) -> None:
        (self.root / "assets").mkdir()
        link = self.root / "link"
        file_symlink(link, "missing")
        self.run_ln("-s", "assets", link.name, expected=1)
        self.assertTrue(link.is_symlink())
        self.assertFalse(link.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows junction test")
    def test_junction_refuses_a_directory_created_during_creation(self) -> None:
        from hagency_cli.files import links

        target = self.root / "assets"
        target.mkdir()
        (target / "keep").touch()
        link = self.root / "link"
        run = links.subprocess.run

        def create_competing_directory(*args, **kwargs):
            link.mkdir()
            return run(*args, **kwargs)

        with mock.patch.object(
            links.subprocess, "run", side_effect=create_competing_directory
        ):
            self.run_ln("-s", "assets", "link", expected=1)

        self.assertTrue(link.is_dir())
        self.assertFalse(is_junction(link))
        self.assertTrue((target / "keep").is_file())
        self.assertCountEqual(self.root.iterdir(), [target, link])
