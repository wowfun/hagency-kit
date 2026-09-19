from __future__ import annotations

import stat
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.cli import app
from hagency_cli.files.cleanup import agents, scan
from hagency_cli.files.cleanup.conditions import evaluate, parse_condition
from hagency_cli.files.cleanup.file_filters import (
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    is_image_name,
    is_text_name,
    is_video_name,
)
from hagency_cli.files.cleanup.models import CleanRequest, ItemDisposition, PurgeRequest
from hagency_cli.files.cleanup.operations import clean_files, purge_space
from hagency_cli.files.cleanup.selection import CleanupSelectionError


class ConditionTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    file = fixtures.CleanupTests.file
    write_rules = fixtures.CleanupTests.write_rules
    versions = fixtures.CleanupTests.versions
    old = fixtures.CleanupTests.old
    project = fixtures.CleanupTests.project

    def run_rule(self, root=None, rule="test", **kwargs):
        return clean_files(
            CleanRequest(
                config=self.rules_file,
                rules=(rule,),
                paths=(root,) if root else (),
                **kwargs,
            ),
            ui=fixtures.CleanupUI(),
        )

    def test_media_names_are_case_insensitive_last_suffix_only(self):
        for classify, suffixes in (
            (is_image_name, IMAGE_SUFFIXES),
            (is_video_name, VIDEO_SUFFIXES),
        ):
            for suffix in suffixes:
                with self.subTest(suffix=suffix):
                    self.assertTrue(classify(Path("absent/file" + suffix.upper())))
                    self.assertFalse(classify(Path("file" + suffix + ".backup")))
            self.assertFalse(classify(Path("unknown")))
        self.assertTrue(is_text_name(Path("diagram.svg")))
        self.assertTrue(is_image_name(Path("diagram.svg")))
        self.assertTrue(is_text_name(Path("code.ts")))
        self.assertFalse(is_video_name(Path("code.ts")))

    def test_negation_preserves_file_domain_at_every_level(self):
        for data in (
            "!is_image=true",
            "!(is_image=true & is_text=true)",
            "!!is_video=false",
            "!(file_size >= 0)",
        ):
            node = parse_condition(data, self.repo, "clean")
            self.assertFalse(
                evaluate(
                    node,
                    self.repo / "folder",
                    stat.S_IFDIR,
                    4096,
                    self.repo,
                    "paths",
                    lambda p: False,
                )
            )
        self.write_rules("""[[rules]]
id="clean.test"
match='!is_image=true'
""")
        text = self.file(self.base / "cache/nested/note.txt")
        image = self.file(text.parent / "photo.JPG")
        report = self.run_rule(text.parent.parent)
        self.assertEqual([choice.exact_path for choice in report.choices], [text])
        self.assertTrue(image.exists())

    def test_empty_directory_condition_selects_directories_and_revalidates(self):
        self.write_rules("""[[rules]]
id="clean.test"
preselect_after_days=0
match='is_empty_directory=true'
""")
        root = self.base / "cache"
        empty = root / "empty"
        empty.mkdir(parents=True)
        self.file(root / "populated/keep")
        regular = self.file(root / "regular.txt")
        preview = self.run_rule(root)
        self.assertEqual([choice.exact_path for choice in preview.choices], [empty])
        self.assertEqual(preview.choices[0].entry_kind, "directory")
        self.assertTrue(regular.exists())

        report = clean_files(
            CleanRequest(
                config=self.rules_file,
                rules=("test",),
                paths=(root,),
            ),
            ui=fixtures.CleanupUI(
                interactive=True,
                hook=lambda: self.file(empty / "arrived"),
            ),
        )
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue(empty.exists())

    def test_empty_directory_condition_stops_after_first_entry(self):
        class OneEntry:
            calls = 0

            def __next__(self):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError(
                        "empty-directory check read past the first entry"
                    )
                return object()

        class Scan:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return self.entries

            def __exit__(self, *args):
                return False

        directory = self.base / "directory"
        directory.mkdir()
        node = parse_condition("is_empty_directory=true", self.repo, "clean")
        entries = OneEntry()
        with mock.patch(
            "hagency_cli.files.cleanup.conditions.os.scandir",
            return_value=Scan(entries),
        ):
            self.assertFalse(
                evaluate(
                    node,
                    directory,
                    stat.S_IFDIR,
                    directory.stat().st_size,
                    self.base,
                    "paths",
                    lambda path: False,
                )
            )
        self.assertEqual(entries.calls, 1)

    def test_builtin_empty_directories_rule_is_explicit_and_path_driven(self):
        with self.assertRaisesRegex(CleanupSelectionError, "PATH"):
            clean_files(
                CleanRequest(config=self.rules_file, rules=("empty_directories",)),
                ui=fixtures.CleanupUI(),
            )
        root = self.base / "cache"
        empty = root / "empty"
        empty.mkdir(parents=True)
        preview = clean_files(
            CleanRequest(
                config=self.rules_file,
                rules=("empty_directories",),
                paths=(root,),
            ),
            ui=fixtures.CleanupUI(),
        )
        self.assertEqual(preview.selected_paths, (empty,))
        report = clean_files(
            CleanRequest(
                config=self.rules_file,
                rules=("empty_directories",),
                paths=(root,),
                yes=True,
            ),
            ui=fixtures.CleanupUI(),
        )
        self.assertEqual([choice.exact_path for choice in report.choices], [empty])
        self.assertFalse(empty.exists())

    def test_nested_media_refs_share_scan_and_do_not_select_dependencies_alone(self):
        self.write_rules("""[[rules]]
id="clean.media"
match='is_image=true | is_video=true'
[[rules]]
id="clean.test"
match='ref="clean.media" & file_size >= 2'
[[rules]]
id="clean.unselected"
min_age_days=30
match='glob="**/*"'
""")
        root = self.base / "cache"
        wanted = self.file(root / "video.MP4", "xx")
        self.file(root / "small.png", "x")
        self.file(root / "note.txt", "xx")
        with mock.patch.object(scan, "_scan_root", wraps=scan._scan_root) as scanning:
            report = self.run_rule(root)
        self.assertEqual(scanning.call_count, 1)
        self.assertEqual([choice.exact_path for choice in report.choices], [wanted])

    def test_any_branches_keep_their_own_within_and_glob_base(self):
        self.write_rules("""[[rules]]
id="clean.test"
match='(within="one" & glob="sub/*.png") | (within="two" & glob="*.mp4")'
""")
        first = self.file(self.repo / "one/sub/photo.png")
        second = self.file(self.repo / "two/video.mp4")
        self.file(self.repo / "one/sub/video.mp4")
        self.file(self.repo / "two/photo.png")
        report = self.run_rule()
        self.assertEqual(
            {choice.exact_path for choice in report.choices}, {first, second}
        )
        narrowed = self.run_rule(first.parent)
        self.assertEqual([choice.exact_path for choice in narrowed.choices], [first])

    def test_ref_own_entry_and_inherited_entry_are_both_supported(self):
        self.write_rules("""[[rules]]
id="clean.anchored"
match='within="one" & glob="*.png"'
[[rules]]
id="clean.leaf"
match='is_video=true'
[[rules]]
id="clean.test"
match='ref="clean.anchored" | (within="two" & ref="clean.leaf")'
""")
        first = self.file(self.repo / "one/photo.png")
        second = self.file(self.repo / "two/video.mp4")
        self.assertEqual(
            {c.exact_path for c in self.run_rule().choices}, {first, second}
        )

    def test_invalid_graphs_and_conditions_fail_without_scan(self):
        cases = (
            "match='ref=\"clean.missing\"'",
            "match='ref=\"purge.missing\"'",
            "match='ref=\"clean.test\"'",
            "match={any=[]}",
            "match={all=[]}",
            "match={not=[]}",
            'match={glob="*",is_text=true}',
            "match='is_image=1'",
            "match='is_video=\"false\"'",
            "match='is_empty_directory=1'",
            "match='unknown=true'",
            'match=\'within="one" & within="two" & glob="*"\'',
            """match='ref="clean.second"'
[[rules]]
id="clean.second"
match='ref="clean.test"'""",
        )
        for body in cases:
            with (
                self.subTest(body=body),
                mock.patch.object(
                    scan, "_scan_root", side_effect=AssertionError("scanned")
                ),
            ):
                self.write_rules('[[rules]]\nid="clean.test"\n' + body)
                report = self.run_rule(self.base)
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(report.issues[0].code, "config_error")

    def test_disabled_reference_rejected_even_under_not(self):
        self.write_rules("""[[rules]]
id="clean.disabled"
enabled=false
match='is_image=true'
[[rules]]
id="clean.test"
match='!ref="clean.disabled"'
""")
        with self.assertRaisesRegex(CleanupSelectionError, "disabled"):
            self.run_rule(self.base)

    def test_negated_entry_does_not_authorize_unbounded_scan(self):
        self.write_rules("""[[rules]]
id="clean.test"
match='!within="keep"'
""")
        with self.assertRaisesRegex(CleanupSelectionError, "PATH"):
            self.run_rule()

    def test_referenced_excludes_and_age_cannot_be_bypassed_by_any(self):
        self.write_rules("""[[rules]]
id="clean.protected"
exclude=["keep.png"]
min_age_days=30
match='is_image=true'
[[rules]]
id="clean.test"
match='ref="clean.protected" | glob="*.png" | is_video=true'
""")
        root = self.base / "cache"
        self.file(root / "keep.png")
        self.file(root / "recent.png")
        video = self.file(root / "video.mp4")
        self.assertEqual(
            [choice.exact_path for choice in self.run_rule(root).choices], [video]
        )

    def test_referenced_descendant_protects_whole_directory(self):
        self.write_rules("""[[rules]]
id="clean.images"
min_age_days=30
match='is_image=true'
[[rules]]
id="clean.test"
match='glob="bundle/" | ref="clean.images"'
""")
        self.file(self.base / "cache/bundle/photo.png")
        self.assertEqual(self.run_rule(self.base / "cache").choices, ())

    def test_agent_ref_and_generic_branch_share_flow_and_keep_inventory_protection(
        self,
    ):
        versions = self.versions(
            "claude", ["1.0.0", "1.1.0", "1.2.0", "1.3.0"], "1.3.0"
        )
        self.write_rules("""[[rules]]
id="clean.claude.versions"
keep_previous=2
[[rules]]
id="clean.test"
match='ref="clean.claude.versions" | (within="media" & is_image=true)'
""")
        image = self.file(self.repo / "media/photo.jpg")
        with mock.patch.object(
            agents, "version_inventory", wraps=agents.version_inventory
        ) as inventory:
            report = self.run_rule()
        self.assertEqual(inventory.call_count, 1)
        self.assertEqual(
            {c.exact_path for c in report.choices}, {versions["1.0.0"], image}
        )
        restricted = self.run_rule(image.parent)
        self.assertEqual([c.exact_path for c in restricted.choices], [image])

    def test_agent_filters_apply_to_inventory_entries_and_revalidate(self):
        versions = self.versions("claude", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        self.write_rules("""[[rules]]
id="clean.test"
match='ref="clean.claude.versions" & is_image=false & file_size >= 0'
""")
        request = CleanRequest(config=self.rules_file, rules=("test",))
        launcher = self.home / ".local/bin/claude"

        def switch():
            launcher.unlink()
            launcher.symlink_to(versions["1.0.0"])

        report = clean_files(
            request, ui=fixtures.CleanupUI(interactive=True, hook=switch)
        )
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue(versions["1.0.0"].exists())

    def test_purge_cachedir_ref_and_depth_keep_special_checks(self):
        root = self.project()
        tagged = self.file(
            root / "cache/CACHEDIR.TAG", "Signature: 8a477f597d28d172789f06886806bc55"
        ).parent
        self.file(tagged / "payload")
        self.write_rules("""[[rules]]
id="purge.test"
match='ref="purge.cachedirs"'
""")
        request = PurgeRequest(paths=(root,), rules=("test",), config=self.rules_file)
        report = purge_space(
            request,
            ui=fixtures.CleanupUI(
                interactive=True,
                hook=lambda: (tagged / "CACHEDIR.TAG").write_text("invalid"),
            ),
        )
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue(tagged.exists())

    def test_rule_query_retains_refs_and_canonical_sizes_without_scanning(self):
        self.write_rules("""[[rules]]
id="clean.media"
match='is_image=true'
[[rules]]
id="clean.test"
match='ref="clean.media" & file_size >= "1.5MiB"'
""")
        with mock.patch.object(
            scan, "_scan_root", side_effect=AssertionError("scanned")
        ):
            result = CliRunner().invoke(
                app, ["file", "rule", "clean.test", "--config", str(self.rules_file)]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(r"ref=\"clean.media\"", result.output)
        self.assertIn("file_size >= 1572864", result.output)

    def test_unanchored_any_branch_is_not_hidden_by_an_anchored_sibling(self):
        self.write_rules("""[[rules]]
id="clean.test"
match='(within="media" | glob="*.png") & is_image=true'
""")
        self.file(self.repo / "media/photo.png")
        with self.assertRaisesRegex(CleanupSelectionError, "PATH"):
            self.run_rule()

    def test_glob_outside_multiple_entries_is_ambiguous(self):
        self.write_rules("""[[rules]]
id="clean.test"
match='(within="one" | within="two") & glob="*.png"'
""")
        report = self.run_rule(self.base)
        self.assertEqual(report.issues[0].code, "config_error")

    def test_diamond_reference_graph_evaluates_each_leaf_once(self):
        from hagency_cli.files.cleanup import conditions

        self.write_rules(
            """[[rules]]
id="clean.leaf"
match='is_image=true'
"""
            + "\n".join(
                f"""[[rules]]\nid="clean.level{i}"\nmatch='ref="clean.{previous}" | ref="clean.{previous}"' """
                for i, previous in (
                    (i, "leaf" if i == 0 else f"level{i - 1}") for i in range(20)
                )
            )
        )
        target = self.file(self.base / "cache/non-image.bin")
        with mock.patch.object(
            conditions, "is_image_name", wraps=is_image_name
        ) as classify:
            report = self.run_rule(target.parent, "level19")
        self.assertEqual(report.choices, ())
        self.assertEqual(classify.call_count, 1)

    def test_project_and_cachedir_can_be_conjoined(self):
        root = self.project()
        tagged = self.file(
            root / "cache/CACHEDIR.TAG", "Signature: 8a477f597d28d172789f06886806bc55"
        ).parent
        self.file(tagged / "payload")
        self.write_rules("""[[rules]]
id="purge.test"
match='source="project" & source="cachedir-tag"'
""")
        report = purge_space(
            PurgeRequest(paths=(root,), rules=("test",), config=self.rules_file),
            ui=fixtures.CleanupUI(),
        )
        self.assertEqual([choice.exact_path for choice in report.choices], [tagged])

    def test_reference_retention_is_scoped_to_its_agent(self):
        claude = self.versions("claude", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        self.write_rules("""[[rules]]
id="clean.codex.versions"
keep_previous=100
[[rules]]
id="clean.test"
match='ref="clean.codex.versions" | ref="clean.claude.versions"'
""")
        report = self.run_rule()
        self.assertEqual(
            [choice.exact_path for choice in report.choices], [claude["1.0.0"]]
        )

    def test_nested_glob_cannot_hide_ambiguous_entry_binding(self):
        self.write_rules("""[[rules]]
id="clean.test"
match='(within="one" | within="two") & (glob="*.png" & is_image=true)'
""")
        report = self.run_rule(self.base)
        self.assertEqual(report.issues[0].code, "config_error")

    def test_combined_reference_depth_is_bounded_before_scanning(self):
        self.write_rules(
            "[[rules]]\nid=\"clean.leaf\"\nmatch='is_text=true'\n"
            + "\n".join(
                f"""[[rules]]\nid="clean.level{i}"\nmatch='ref="clean.{previous}" & is_text=true' """
                for i, previous in (
                    (i, "leaf" if i == 0 else f"level{i - 1}") for i in range(40)
                )
            )
        )
        report = self.run_rule(self.base, "level39")
        self.assertEqual(report.issues[0].code, "config_error")
        self.assertIn("nesting", report.issues[0].message)

    def test_path_intersection_skips_unrelated_agent_inventory(self):
        self.versions("claude", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        image = self.file(self.repo / "media/photo.png")
        self.write_rules("""[[rules]]
id="clean.test"
match='ref="clean.claude.versions" | (within="media" & is_image=true)'
""")
        with mock.patch.object(
            agents,
            "version_inventory",
            side_effect=AssertionError("unrelated Agent inventory"),
        ):
            report = self.run_rule(image.parent)
        self.assertEqual([choice.exact_path for choice in report.choices], [image])

    def test_reference_exclusions_do_not_cross_agent_sources(self):
        versions = self.versions("claude", ["1.0.0", "1.1.0", "1.2.0"], "1.2.0")
        self.write_rules("""[[rules]]
id="clean.codex.versions"
exclude=["*"]
[[rules]]
id="clean.test"
match='ref="clean.codex.versions" | ref="clean.claude.versions"'
""")
        report = self.run_rule()
        self.assertEqual(
            [choice.exact_path for choice in report.choices], [versions["1.0.0"]]
        )
