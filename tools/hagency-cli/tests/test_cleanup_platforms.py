from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.files.cleanup import config


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
