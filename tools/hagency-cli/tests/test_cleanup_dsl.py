from __future__ import annotations

import json
import stat
import tomllib
import unittest
from unittest import mock

from typer.testing import CliRunner

from tests import test_cleanup as fixtures

# isort: split

from hagency_cli.cli import app
from hagency_cli.files.cleanup import agents, config, scan
from hagency_cli.files.cleanup.conditions import (
    Condition,
    condition_depth,
    evaluate,
    parse_condition,
)
from hagency_cli.files.cleanup.models import CleanRequest
from hagency_cli.files.cleanup.operations import clean_files


class ExpressionTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    write_rules = fixtures.CleanupTests.write_rules

    def parse(self, expression):
        return parse_condition(expression, self.repo, "clean")

    def matches(self, expression, *, name="file.bin", size=0, mode=stat.S_IFREG):
        return evaluate(
            self.parse(expression),
            self.repo / name,
            mode,
            size,
            self.repo,
            "paths",
            lambda path: False,
        )

    def test_boolean_shorthand_normalizes_explicit_values_and_roundtrips(self):
        for predicate in ("is_text", "is_image", "is_video", "is_empty_directory"):
            for short, explicit, canonical in (
                (predicate, predicate + " = true", predicate),
                ("!" + predicate, predicate + " = false", "!" + predicate),
                ("!!" + predicate, "!" + predicate + "=false", predicate),
            ):
                with self.subTest(short=short):
                    node = self.parse(explicit)
                    self.assertEqual(self.parse(short), node)
                    self.assertEqual(node.expression(), canonical)
                    self.assertEqual(self.parse(node.expression()), node)
        self.assertEqual(
            self.parse("(is_text | is_image) & !is_video"),
            self.parse("(is_text=true | is_image=true) & is_video=false"),
        )
        self.assertEqual(
            self.parse('glob="is_text=true"').expression(), 'glob="is_text=true"'
        )

    def test_boolean_shorthand_preserves_file_and_directory_domains(self):
        paths = []
        for name in ("note.txt", "photo.png", "movie.mp4", "unknown.bin"):
            path = self.repo / name
            path.write_text("payload")
            paths.append(path)
        for name in ("empty", "populated"):
            path = self.repo / name
            path.mkdir()
            paths.append(path)
        (self.repo / "populated/child").write_text("payload")
        for predicate in ("is_text", "is_image", "is_video", "is_empty_directory"):
            for path in paths:
                info = path.stat()
                args = {"name": path.name, "mode": info.st_mode, "size": info.st_size}
                with self.subTest(predicate=predicate, path=path.name):
                    self.assertEqual(
                        self.matches(predicate, **args),
                        self.matches(predicate + "=true", **args),
                    )
                    self.assertEqual(
                        self.matches("!" + predicate, **args),
                        self.matches(predicate + "=false", **args),
                    )
                    if path.is_dir() != (predicate == "is_empty_directory"):
                        self.assertFalse(self.matches(predicate, **args))
                        self.assertFalse(self.matches("!" + predicate, **args))
        self.assertTrue(
            self.matches("is_empty_directory", name="empty", mode=stat.S_IFDIR)
        )
        self.assertTrue(
            self.matches("!is_empty_directory", name="populated", mode=stat.S_IFDIR)
        )

    def test_empty_directory_shorthand_preserves_cascade_scope_selection(self):
        for expression, scope_count in (
            ("is_empty_directory=false", 0),
            ("!is_empty_directory", 0),
            ("!!!is_empty_directory", 0),
            ("is_empty_directory", 1),
            ("!!is_empty_directory", 1),
            ("!is_empty_directory=false", 1),
            ('!ref="clean.empty_directories"', 0),
            ('!!ref="clean.empty_directories"', 1),
            ('!ref="clean.nonempty"', 1),
            ('!(glob="cache/" | is_empty_directory)', 0),
            ('!(glob="cache/" | !is_empty_directory)', 1),
            ('!ref="clean.empty_directories" | ref="clean.empty_directories"', 1),
            ('ref="clean.empty_directories" | !ref="clean.empty_directories"', 1),
        ):
            with self.subTest(expression=expression):
                self.write_rules(
                    '[[rules]]\nid="clean.nonempty"\nmatch="!is_empty_directory"\n'
                    '[[rules]]\nid="clean.directories"\nmatch=' + json.dumps(expression)
                )
                plan = scan._build_plan(
                    CleanRequest(
                        config=self.rules_file,
                        paths=(self.repo,),
                        rules=("directories",),
                    )
                )
                self.assertEqual(len(plan.empty_directory_scopes), scope_count)

    def test_precedence_parentheses_and_negation_apply_to_complete_conditions(self):
        cases = (
            ('glob="*.txt" | is_image=true & file_size > 5', "note.txt", 0, True),
            ('(glob="*.txt" | is_image=true) & file_size > 5', "note.txt", 0, False),
            ('glob="*.txt" | is_image=true & file_size > 5', "photo.png", 5, False),
            ('glob="*.txt" | is_image=true & file_size > 5', "photo.png", 6, True),
            ("!is_image=true & is_text=true", "photo.svg", 0, False),
            ("!is_image=true & is_text=true", "note.txt", 0, True),
            ("!(is_image=true & is_text=true)", "file.bin", 0, True),
            ("!!is_image=true", "photo.png", 0, True),
            ("!!!is_image=true", "photo.png", 0, False),
        )
        for expression, name, size, expected in cases:
            with self.subTest(expression=expression, name=name):
                self.assertEqual(
                    self.matches(expression, name=name, size=size), expected
                )

    def test_all_six_size_comparisons_and_strict_boundaries(self):
        for comparison, matches in (
            ("<", {0, 1}),
            ("<=", {0, 1, 2}),
            ("=", {2}),
            ("!=", {0, 1, 3}),
            (">=", {2, 3}),
            (">", {3}),
        ):
            for size in range(4):
                with self.subTest(comparison=comparison, size=size):
                    self.assertEqual(
                        self.matches(f"file_size {comparison} 2", size=size),
                        size in matches,
                    )
        maximum = 2**63 - 1
        for expression, size, expected in (
            ("file_size < 0", 0, False),
            ("file_size = 0", 0, True),
            ("file_size > 0", 1, True),
            (f"file_size > {maximum}", maximum, False),
            (f"file_size >= {maximum}", maximum, True),
            (f"file_size < {maximum}", maximum - 1, True),
            (f"file_size != {maximum}", maximum, False),
            ("file_size >= 2 & file_size <= 1", 1, False),
            ("!(file_size >= 2 & file_size <= 1)", 1, True),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(self.matches(expression, size=size), expected)

    def test_size_and_type_negation_never_select_directories_or_links(self):
        for expression in (
            "file_size != 0",
            "!(file_size <= 1MB)",
            "!!(file_size > 0)",
            "!(file_size < 0 | is_image=true)",
            "!(file_size >= 2 & file_size <= 1)",
        ):
            for mode in (stat.S_IFDIR, stat.S_IFLNK):
                with self.subTest(expression=expression, mode=mode):
                    self.assertFalse(
                        self.matches(expression, size=2_000_000, mode=mode)
                    )

    def test_units_and_integer_padding_use_exact_bounded_byte_conversion(self):
        for value, size in (
            ("1024", 1024),
            ("1MB", 1_000_000),
            ("1.5MiB", 1_572_864),
            ('"1.5MiB"', 1_572_864),
            ("'1.5MiB'", 1_572_864),
            ("1.5 mIb", 1_572_864),
            ("0.001KB", 1),
            ("0" * 10_000 + "1", 1),
            ("0.0000000000009094947017729282379150390625TiB", 1),
        ):
            with self.subTest(value=value[:80]):
                self.assertEqual(self.parse("file_size = " + value).value, ("=", size))

    def test_quoted_operators_quotes_backslashes_and_unicode_are_literals(self):
        for literal, expected in (
            ('"a|b&c!(d)"', "a|b&c!(d)"),
            ("'a|b&c!(d)'", "a|b&c!(d)"),
            (r'''"a\"b'c\\d"''', "a\"b'c\\d"),
            (r"'a\b'", "a\\b"),
            (r'"\u4e2d\u6587.txt"', "中文.txt"),
        ):
            with self.subTest(literal=literal):
                node = self.parse("glob=" + literal)
                self.assertEqual(node.value, expected)
                self.assertEqual(self.parse(node.expression()), node)

    def test_roundtrip_preserves_lexical_groups_refs_and_canonical_bytes(self):
        for expression in (
            'within="one" & (glob="*.png" & is_image=true)',
            '(within="one" & glob="*.png") | (within="two" & glob="*.mp4")',
            'ref="clean.media" & !(is_text=false | file_size < 1.5MiB)',
            '!(ref="purge.artifacts" | !!is_empty_directory=false)',
            "\n\tfile_size >= 1MB &\n (is_text=true | is_image=true)\n",
        ):
            with self.subTest(expression=expression):
                node = self.parse(expression)
                rendered = node.expression()
                self.assertEqual(self.parse(rendered), node)
                self.assertEqual(
                    self.parse(tomllib.loads("match=" + json.dumps(rendered))["match"]),
                    node,
                )
        node = self.parse('within="one" & (glob="*.png" & is_image=true)')
        self.assertEqual(node.children[1].op, "all")
        self.assertEqual(len(node.children), 2)
        self.assertEqual(
            self.parse('file_size <= "1.5MiB"').expression(), "file_size <= 1572864"
        )

    def test_flat_chains_stay_shallow_and_nesting_is_bounded(self):
        for separator, op in ((" & ", "all"), (" | ", "any")):
            node = self.parse(separator.join(["is_text=true"] * 2000))
            self.assertEqual(node.op, op)
            self.assertEqual(len(node.children), 2000)
            self.assertEqual(condition_depth(node), 2)
        self.assertEqual(self.parse("(" * 64 + "is_text=true" + ")" * 64).op, "is_text")
        self.assertEqual(self.parse("!" * 63 + "is_text=true"), self.parse("!is_text"))
        for expression in (
            "(" * 65 + "is_text=true" + ")" * 65,
            "!" * 64 + "is_text=true",
            "(" * 10_000,
            "!" * 10_000,
        ):
            with (
                self.subTest(prefix=expression[:80]),
                self.assertRaisesRegex(ValueError, "64 levels"),
            ):
                self.parse(expression)

    def test_invalid_expressions_and_legacy_tables_fail_before_scanning(self):
        values = (
            "",
            " \n ",
            "()",
            "!",
            "| is_text=true",
            "is_text=true &",
            "is_text=true || is_image=true",
            "is_text=true && is_image=true",
            "(is_text=true",
            "is_text=true)",
            "is_text=true is_image=true",
            'glob="unterminated',
            r'glob="bad\q"',
            'glob=""',
            r'glob="bad\u007f"',
            r'within="bad\u202e"',
            "glob=*.tmp",
            'glob!="*.tmp"',
            'is_text="true"',
            "is_text=1",
            "is_empty_directory=TRUE",
            "is_text is_image",
            "is_text < true",
            "is_text=",
            "is_text!",
            "glob",
            "within",
            "ref",
            "source",
            "file_size",
            "unknown",
            "unknown=true",
            'ref="media"',
            'source="project"',
            "min_file_size=1",
            "max_file_size=1",
            "file_size == 1",
            "file_size => 1",
            "file_size =",
            "file_size=true",
            "file_size=-1",
            "file_size=1.5",
            "file_size=0.1B",
            "file_size=1XB",
            "file_size=9223372036854775808",
            'file_size="1024"',
            "file_size=" + "9" * 10_000,
            {"glob": "*.tmp"},
            {"all": [{"is_text": True}]},
        )
        with (
            mock.patch.object(
                scan, "_scan_root", side_effect=AssertionError("scanned")
            ),
            mock.patch.object(
                agents, "source_roots", side_effect=AssertionError("inventoried")
            ),
        ):
            for value in values:
                with self.subTest(value=str(value)[:100]):
                    if isinstance(value, dict):
                        encoded = (
                            '{glob="*.tmp"}'
                            if "glob" in value
                            else "{all=[{is_text=true}]}"
                        )
                    else:
                        encoded = json.dumps(value)
                    self.write_rules('[[rules]]\nid="clean.invalid"\nmatch=' + encoded)
                    report = clean_files(
                        CleanRequest(
                            config=self.rules_file,
                            paths=(self.repo,),
                            rules=("invalid",),
                        ),
                        ui=fixtures.CleanupUI(),
                    )
                    self.assertEqual(report.exit_code, 1)
                    self.assertEqual(report.issues[0].code, "config_error")
                    message = report.issues[0].message
                    self.assertIn("clean.invalid", message)
                    self.assertIn(str(self.rules_file), message)
                    self.assertRegex(message, r"line \d+, column \d+")

    def test_error_position_uses_decoded_expression_lines(self):
        self.write_rules(
            '[[rules]]\nid="clean.invalid"\nmatch='
            + json.dumps("file_size >= 0 &\n  is_text=maybe")
        )
        with self.assertRaisesRegex(config.CleanupConfigError, "line 2, column 11"):
            config.load_rules(self.rules_file)

    def test_rule_query_emits_copyable_expression_without_target_inspection(self):
        self.write_rules("""[[rules]]
id="clean.images"
match='is_image=true'
[[rules]]
id="clean.filtered"
match='ref="clean.images" & (file_size >= 1MiB & file_size < 2MB) & is_text=false'
""")
        with (
            mock.patch.object(
                scan, "_scan_root", side_effect=AssertionError("scanned")
            ),
            mock.patch.object(
                agents, "source_roots", side_effect=AssertionError("inventoried")
            ),
        ):
            result = CliRunner().invoke(
                app,
                ["file", "rule", "clean.filtered", "--config", str(self.rules_file)],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        detail = tomllib.loads(result.output)
        self.assertEqual(
            detail["match"],
            'ref="clean.images" & (file_size >= 1048576 & file_size < 2000000) & !is_text',
        )
        self.assertEqual(detail["references"], ["clean.images"])
        snapshot = config.load_rules(self.rules_file)
        original = next(rule for rule in snapshot.rules if rule.id == "clean.filtered")
        self.assertEqual(self.parse(detail["match"]), original.match)

    def test_size_node_rejects_invalid_direct_construction(self):
        for value in (("==", 0), (">", -1), ("<=", 2**63), ("=", True), (None, 0)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Condition("file_size", value)
