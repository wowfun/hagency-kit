from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from hagency_cli import paths


class UserDirectoryTests(unittest.TestCase):
    def test_defaults_and_invalid_values_are_identical_across_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for platform in ("win32", "darwin", "linux"):
                for value in (
                    None,
                    "",
                    "relative",
                    "../parent",
                    "~/custom",
                    "$HOME/custom",
                ):
                    with self.subTest(platform=platform, value=value):
                        environ = (
                            {}
                            if value is None
                            else {
                                "XDG_CONFIG_HOME": value,
                                "XDG_STATE_HOME": value,
                            }
                        )
                        with mock.patch("sys.platform", platform):
                            self.assertEqual(
                                paths.user_config_dir(environ=environ, home=home),
                                home / ".config/hagency",
                            )
                            self.assertEqual(
                                paths.user_state_dir(environ=environ, home=home),
                                home / ".local/state/hagency",
                            )
            self.assertFalse(home.exists())

    def test_absolute_xdg_roots_override_legacy_variables_on_every_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config = Path(tmp) / "config-root"
            state = Path(tmp) / "state-root"
            environ = {
                "XDG_CONFIG_HOME": str(config),
                "XDG_STATE_HOME": str(state),
                "APPDATA": str(home / "old-config"),
                "LOCALAPPDATA": str(home / "old-local"),
                "HAGENCY_STATE_HOME": str(home / "old-state"),
            }
            for platform in ("win32", "darwin", "linux"):
                with (
                    self.subTest(platform=platform),
                    mock.patch("sys.platform", platform),
                ):
                    self.assertEqual(
                        paths.user_config_dir(environ=environ, home=home),
                        config / "hagency",
                    )
                    self.assertEqual(
                        paths.user_state_dir(environ=environ, home=home),
                        state / "hagency",
                    )
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_process_environment_and_current_user_home_are_used_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with (
                mock.patch.object(paths.Path, "home", return_value=home),
                mock.patch.dict(
                    os.environ, {"XDG_CONFIG_HOME": "", "XDG_STATE_HOME": ""}
                ),
            ):
                self.assertEqual(paths.user_config_dir(), home / ".config/hagency")
                self.assertEqual(paths.user_state_dir(), home / ".local/state/hagency")
            with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}
            ):
                self.assertEqual(paths.user_config_dir(), Path(tmp) / "hagency")
                self.assertEqual(paths.user_state_dir(), Path(tmp) / "hagency")

    def test_windows_drive_and_unc_paths_require_absolute_roots(self):
        home = PureWindowsPath("C:/Users/test")
        with mock.patch.object(paths, "Path", PureWindowsPath):
            for value in ("D:/xdg", r"D:\xdg", r"\\server\share\xdg"):
                with self.subTest(value=value):
                    environ = {"XDG_CONFIG_HOME": value, "XDG_STATE_HOME": value}
                    self.assertEqual(
                        paths.user_config_dir(environ=environ, home=home),
                        PureWindowsPath(value) / "hagency",
                    )
                    self.assertEqual(
                        paths.user_state_dir(environ=environ, home=home),
                        PureWindowsPath(value) / "hagency",
                    )
            for value in ("D:relative", r"\root-relative", "/c/xdg", "~/xdg"):
                with self.subTest(value=value):
                    environ = {"XDG_CONFIG_HOME": value, "XDG_STATE_HOME": value}
                    self.assertEqual(
                        paths.user_config_dir(environ=environ, home=home),
                        home / ".config/hagency",
                    )
                    self.assertEqual(
                        paths.user_state_dir(environ=environ, home=home),
                        home / ".local/state/hagency",
                    )

    def test_posix_absolute_paths_do_not_expand_shell_expressions(self):
        home = PurePosixPath("/home/test")
        with mock.patch.object(paths, "Path", PurePosixPath):
            self.assertEqual(
                paths.user_config_dir(
                    environ={"XDG_CONFIG_HOME": "/config/$USER"}, home=home
                ),
                PurePosixPath("/config/$USER/hagency"),
            )
            self.assertEqual(
                paths.user_state_dir(environ={"XDG_STATE_HOME": "C:/xdg"}, home=home),
                home / ".local/state/hagency",
            )


if __name__ == "__main__":
    unittest.main()
