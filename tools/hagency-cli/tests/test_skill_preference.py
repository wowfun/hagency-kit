from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from hagency_cli import cli
from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import SkillNameConflictError, SkillReferenceError
from hagency_cli.workspace.operations.skills import add_skills
from hagency_cli.workspace.profiles import apply_profile
from hagency_cli.workspace.skills import (
    resolve_selector,
    resolve_skill_reference,
)
from hagency_cli.workspace.sources import Source


class SkillPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        # The source root's name must not affect source-relative classification.
        self.source = Source("design", self.root / "plugin-checkouts/design", None)
        self.source.path.mkdir(parents=True)
        self.sources = {self.source.name: self.source}
        self.destination = self.root / "installed"
        self.consumer = self.root / "consumer"
        self.consumer.mkdir()
        self.profile = {"name": "content", "skill": {"design": {}}}
        write_toml(
            self.root / "hagency-config.toml",
            {"source": {"design": {"path": str(self.source.path)}}},
        )

    def skill(self, selector, source=None):
        path = (source or self.source).path / selector
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {path.name}\ndescription: fixture\n---\n",
            encoding="utf-8",
        )
        return path

    def dataviz(self):
        ordinary = self.skill("skills/dataviz")
        plugins = [
            self.skill("chatgpt-plugins/baoyu-dataviz/skills/dataviz"),
            self.skill("chatgpt-plugins/baoyu-motion/skills/dataviz"),
        ]
        return ordinary, plugins

    def apply(self, **kwargs):
        return apply_profile(
            self.profile,
            self.sources,
            self.root,
            self.destination,
            link_mode="copy",
            dry_run=kwargs.pop("dry_run", False),
            **kwargs,
        )

    def add(self, value, **kwargs):
        return add_skills(
            skill=value, root_value=str(self.root), cwd=self.consumer, **kwargs
        )

    def invoke(self, *args):
        with contextlib.chdir(self.root):
            result = CliRunner().invoke(cli.app, list(args), catch_exceptions=False)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result

    def test_profile_apply_automatically_installs_ordinary_dataviz(self):
        ordinary, _plugins = self.dataviz()
        ui = mock.Mock()
        ui.select.side_effect = AssertionError("unique ordinary skill must not prompt")
        selected = self.apply(conflict_ui=ui)
        self.assertEqual([link.target for link in selected], [ordinary])
        self.assertTrue((self.destination / "dataviz/SKILL.md").is_file())
        ui.is_interactive.assert_not_called()
        ui.select.assert_not_called()

    def test_profile_dry_run_resolves_without_terminal_or_writes(self):
        ordinary, _plugins = self.dataviz()
        events = []
        selected = self.apply(dry_run=True, progress=events.append)
        self.assertEqual([link.target for link in selected], [ordinary])
        output = "\n".join(event.message for event in events)
        self.assertIn(str(ordinary), output)
        self.assertNotIn("conflict", output)
        self.assertNotIn("chatgpt-plugins", output)
        self.assertFalse(self.destination.exists())

    def test_preference_runs_after_include_and_exclude(self):
        self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        self.profile["skill"]["design"] = {
            "include": ["dataviz"],
            "exclude": ["skills/dataviz"],
        }
        selected = self.apply()
        self.assertEqual([link.target for link in selected], [plugin])

    def test_profile_exact_path_and_subtree_can_select_plugin(self):
        self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        for selector in ("plugins", "plugins/one/skills/dataviz"):
            with self.subTest(selector=selector):
                self.profile["skill"]["design"] = {"include": [selector]}
                selected = self.apply(dry_run=True)
                self.assertEqual([link.target for link in selected], [plugin])

    def test_profile_explicit_twins_remain_a_conflict_before_writes(self):
        self.skill("skills/dataviz")
        self.skill("plugins/one/skills/dataviz")
        self.profile["skill"]["design"] = {
            "include": ["skills/dataviz", "plugins/one/skills/dataviz"]
        }
        with self.assertRaises(SkillNameConflictError):
            self.apply()
        self.assertFalse(self.destination.exists())

    def test_profile_explicit_twins_are_visible_in_dry_run(self):
        ordinary = self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        self.profile["skill"]["design"] = {
            "include": ["skills/dataviz", "plugins/one/skills/dataviz"]
        }
        events = []
        selected = self.apply(dry_run=True, progress=events.append)
        self.assertEqual(selected, ())
        output = "\n".join(event.message for event in events)
        self.assertIn("conflict 'dataviz'", output)
        self.assertIn(str(ordinary), output)
        self.assertIn(str(plugin), output)
        self.assertFalse(self.destination.exists())

    def test_explicit_plugin_survives_overlapping_wildcard_in_either_order(self):
        ordinary = self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        implicit_plugin = self.skill("plugins/two/skills/dataviz")
        for selectors in (
            ["*", "plugins/one/skills/dataviz"],
            ["plugins/one/skills/dataviz", "*"],
        ):
            with self.subTest(selectors=selectors):
                self.profile["skill"]["design"] = {"include": selectors}
                events = []
                self.apply(dry_run=True, progress=events.append)
                output = "\n".join(event.message for event in events)
                self.assertIn("conflict 'dataviz'", output)
                self.assertIn(str(ordinary), output)
                self.assertIn(str(plugin), output)
                self.assertNotIn(str(implicit_plugin), output)

    def test_profile_explicit_plugin_can_win_conflict(self):
        ordinary = self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        self.profile["skill"]["design"] = {
            "include": ["dataviz", "plugins/one/skills/dataviz"]
        }
        ui = mock.Mock()
        ui.select.return_value = plugin
        selected = self.apply(conflict_ui=ui)
        self.assertEqual([link.target for link in selected], [plugin])
        self.assertEqual(
            {link.target for link in ui.select.call_args.args[1]}, {ordinary, plugin}
        )

    def test_multiple_ordinary_candidates_keep_existing_conflict_flow(self):
        first = self.skill("skills/dataviz")
        second = self.skill("other/dataviz")
        self.skill("plugins/one/dataviz")
        with self.assertRaises(SkillNameConflictError):
            self.apply()
        self.assertFalse(self.destination.exists())
        ui = mock.Mock()
        ui.select.return_value = second
        selected = self.apply(conflict_ui=ui)
        self.assertEqual([link.target for link in selected], [second])
        offered = ui.select.call_args.args[1]
        self.assertEqual({link.target for link in offered}, {first, second})

    def test_all_plugin_candidates_still_require_choice(self):
        first = self.skill("plugins/one/dataviz")
        second = self.skill("chatgpt-plugins/two/dataviz")
        with self.assertRaises(SkillNameConflictError):
            self.apply()
        ui = mock.Mock()
        ui.select.return_value = first
        selected = self.apply(conflict_ui=ui)
        self.assertEqual([link.target for link in selected], [first])
        self.assertEqual(
            {link.target for link in ui.select.call_args.args[1]}, {first, second}
        )

    def test_preference_does_not_choose_between_sources(self):
        ordinary, _plugins = self.dataviz()
        other = Source("other", self.root / "other", None)
        plugin = self.skill("plugins/dataviz", other)
        self.sources[other.name] = other
        self.profile["skill"][other.name] = {}
        with self.assertRaises(SkillNameConflictError):
            self.apply()
        ui = mock.Mock()
        ui.select.return_value = plugin
        self.apply(conflict_ui=ui)
        self.assertEqual(
            {(link.source_name, link.target) for link in ui.select.call_args.args[1]},
            {("design", ordinary), ("other", plugin)},
        )

    def test_plugin_keyword_is_case_insensitive_and_source_relative(self):
        ordinary = self.skill("skills/dataviz")
        for parent in ("PLuGiNs", "chatgpt-plugins", "dsh-plugins"):
            self.skill(f"{parent}/one/dataviz")
        selected = self.apply(dry_run=True)
        self.assertEqual([link.target for link in selected], [ordinary])

    def test_bare_name_and_source_selector_resolve_ordinary_skill(self):
        ordinary, _plugins = self.dataviz()
        self.assertEqual(
            resolve_skill_reference("dataviz", self.sources, self.root),
            ("design", "dataviz"),
        )
        self.assertEqual(
            resolve_selector(self.source, "dataviz"), [("dataviz", ordinary)]
        )

    def test_bare_name_keeps_cross_source_ambiguity(self):
        self.dataviz()
        other = Source("other", self.root / "other", None)
        self.skill("plugins/dataviz", other)
        self.sources[other.name] = other
        with self.assertRaises(SkillReferenceError) as error:
            resolve_skill_reference("dataviz", self.sources, self.root)
        self.assertIn("design:skills/dataviz", str(error.exception))
        self.assertIn("other:plugins/dataviz", str(error.exception))
        self.assertNotIn("chatgpt-plugins", str(error.exception))

    def test_profile_add_and_update_resolve_names(self):
        self.dataviz()
        self.invoke("profile", "add", "content", "-AS", "dataviz")
        path = self.root / "profiles/content/config.toml"
        self.assertEqual(read_toml(path)["skill"]["design"]["include"], ["dataviz"])
        self.invoke(
            "profile", "update", "content", "-AS", "design", "-i", "dataviz"
        )
        self.assertEqual(read_toml(path)["skill"]["design"]["include"], ["dataviz"])

    def test_skill_add_all_and_selectors_use_ordinary_candidate(self):
        ordinary, _plugins = self.dataviz()
        for kwargs in ({"all_skills": True}, {"selectors": ("dataviz",)}):
            with self.subTest(kwargs=kwargs):
                report = self.add("design", dry_run=True, **kwargs)
                self.assertEqual([link.target for link in report.selected], [ordinary])
        self.assertFalse((self.consumer / ".agents").exists())
        report = self.add("design", all_skills=True)
        self.assertEqual([link.target for link in report.selected], [ordinary])
        self.assertEqual(
            (self.consumer / ".agents/skills/dataviz").resolve(), ordinary.resolve()
        )

    def test_skill_add_bare_name_and_exact_plugin_reference(self):
        ordinary, plugins = self.dataviz()
        report = self.add("dataviz", dry_run=True)
        self.assertEqual([link.target for link in report.selected], [ordinary])
        selector = plugins[0].relative_to(self.source.path).as_posix()
        report = self.add(f"design:{selector}", dry_run=True)
        self.assertEqual([link.target for link in report.selected], [plugins[0]])

    def test_skill_add_explicit_plugin_is_not_discarded_by_other_selectors(self):
        ordinary = self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        for selectors in (
            ("skills/dataviz", "plugins/one/skills/dataviz"),
            ("dataviz", "plugins/one/skills/dataviz"),
        ):
            with self.subTest(selectors=selectors):
                with self.assertRaises(SkillNameConflictError):
                    self.add("design", selectors=selectors)
                self.assertFalse((self.consumer / ".agents").exists())
                events = []
                report = self.add(
                    "design", selectors=selectors, dry_run=True, progress=events.append
                )
                self.assertEqual(report.selected, ())
                output = "\n".join(event.message for event in events)
                self.assertIn("conflict 'dataviz'", output)
                self.assertIn(str(ordinary), output)
                self.assertIn(str(plugin), output)

    def test_skill_add_picker_preserves_both_explicit_choices(self):
        ordinary = self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        ui = mock.Mock()
        ui.choose_skills.return_value = (ordinary, plugin)
        ui.select.return_value = plugin
        report = self.add("design", ui=ui)
        self.assertEqual([link.target for link in report.selected], [plugin])
        self.assertEqual(
            {link.target for link in ui.select.call_args.args[1]}, {ordinary, plugin}
        )
        self.assertEqual(
            (self.consumer / ".agents/skills/dataviz").resolve(), plugin.resolve()
        )

    def test_skill_add_scoped_subtree_does_not_use_outside_candidate(self):
        self.skill("skills/dataviz")
        plugin = self.skill("plugins/one/skills/dataviz")
        for kwargs in ({"all_skills": True}, {"selectors": ("dataviz",)}):
            with self.subTest(kwargs=kwargs):
                report = self.add(str(plugin.parent), dry_run=True, **kwargs)
                self.assertEqual([link.target for link in report.selected], [plugin])

    def test_preference_keeps_plugin_skills_with_different_names(self):
        ordinary, _plugins = self.dataviz()
        unique = self.skill("plugins/one/skills/animation")
        selected = self.apply(dry_run=True)
        self.assertEqual({link.target for link in selected}, {ordinary, unique})

    def test_skill_list_retains_plugin_selectors(self):
        ordinary, plugins = self.dataviz()
        (self.root / "profiles/content").mkdir(parents=True)
        write_toml(self.root / "profiles/content/config.toml", self.profile)
        for filters in (("--source", "design"), ("--profile", "content")):
            with self.subTest(filters=filters):
                result = self.invoke("skill", "list", *filters)
                for path in (ordinary, *plugins):
                    self.assertIn(
                        path.relative_to(self.source.path).as_posix(), result.stdout
                    )

    def test_skill_list_profile_name_retains_all_candidates(self):
        ordinary, plugins = self.dataviz()
        self.profile["skill"]["design"] = {"include": ["dataviz"]}
        (self.root / "profiles/content").mkdir(parents=True)
        write_toml(self.root / "profiles/content/config.toml", self.profile)
        result = self.invoke("skill", "list", "--profile", "content")
        for path in (ordinary, *plugins):
            self.assertIn(path.relative_to(self.source.path).as_posix(), result.stdout)

    def test_remaining_dry_run_conflicts_only_list_ordinary_candidates(self):
        first = self.skill("one/dataviz")
        second = self.skill("two/dataviz")
        self.skill("plugins/dataviz")
        events = []
        selected = self.apply(dry_run=True, progress=events.append)
        self.assertEqual(selected, ())
        output = "\n".join(event.message for event in events)
        self.assertIn("conflict 'dataviz'", output)
        self.assertIn(str(first), output)
        self.assertIn(str(second), output)
        self.assertNotIn(str(self.source.path / "plugins/dataviz"), output)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
