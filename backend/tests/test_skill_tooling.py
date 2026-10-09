"""Tests for ``skills/tooling.py`` - the validation/testing engine behind the
skill_creator's repair loop and the ``scripts/`` CLIs.

The engine never executes a skill during validation; the tests below assert both
the static checks and the execution stages (unit/functional/integration) against
generated packs under a temporary skills root.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skills import tooling  # noqa: E402
from config import config  # noqa: E402
from tools.base import Tool, ToolContext, ToolRegistry  # noqa: E402
from tools.core_tools import build_core_tools  # noqa: E402


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names or ("web_search", "web_fetch"):
        reg.register(Tool(name, f"{name} test tool.",
                          {"type": "object", "properties": {}},
                          lambda args, ctx: "ok"))
    return reg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_skill(self, name="demo", *, description="A demo skill that does one useful thing.",
                    body="Do the thing carefully.", tools=None, extra=None, pack=False):
        meta = {"name": name, "description": description}
        if tools is not None:
            meta["tools"] = ", ".join(tools)
        if extra:
            meta.update(extra)
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
        (self.root / f"{name}.md").write_text(f"---\n{front}---\n\n{body}\n",
                                              encoding="utf-8")
        if pack:
            (self.root / name).mkdir()
        return self.root / f"{name}.md"

    def write_script(self, skill, filename, content, mode=0o700):
        target = self.root / skill / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, mode)
        return target


class ResolveTests(Base):
    def test_bare_name_resolves_against_user_dir(self):
        self.write_skill("demo")
        md, pack = tooling.resolve_target("demo", user_dir=self.root,
                                          builtin_dir=self.root / "none")
        self.assertEqual(md.name, "demo.md")
        self.assertIsNone(pack)

    def test_pack_dir_resolves_to_sibling_markdown(self):
        self.write_skill("demo", pack=True)
        md, pack = tooling.resolve_target(self.root / "demo", user_dir=self.root,
                                          builtin_dir=self.root)
        self.assertEqual(md.name, "demo.md")
        self.assertEqual(pack, self.root / "demo")

    def test_unknown_name_is_reported(self):
        with self.assertRaises(tooling.ToolingError):
            tooling.resolve_target("ghost", user_dir=self.root,
                                   builtin_dir=self.root / "none")


class ValidateTests(Base):
    def validate(self, name="demo", **kw):
        return tooling.validate_skill(self.root / f"{name}.md", user_dir=self.root)

    def test_good_skill_is_valid(self):
        self.write_skill("demo", tools=["web_search"])
        report = tooling.validate_skill(self.root / "demo.md",
                                        registry=_registry(), user_dir=self.root)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["skill"], "demo")

    def test_missing_frontmatter_is_invalid(self):
        (self.root / "demo.md").write_text("just prose, no frontmatter\n")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])
        self.assertTrue(any("frontmatter" in e for e in report["errors"]))

    def test_bad_name_is_invalid(self):
        self.write_skill("demo", extra={"name": "bad name!"})
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])

    def test_missing_description_is_invalid(self):
        self.write_skill("demo", description="")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])

    def test_empty_prompt_is_invalid(self):
        self.write_skill("demo", body="")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])

    def test_unknown_tool_is_invalid(self):
        self.write_skill("demo", tools=["ghost_tool"])
        report = tooling.validate_skill(self.root / "demo.md",
                                        registry=_registry("web_search"),
                                        user_dir=self.root)
        self.assertFalse(report["valid"])
        self.assertTrue(any("ghost_tool" in e for e in report["errors"]))

    def test_todo_placeholder_is_invalid(self):
        self.write_skill("demo", body="[TODO: write the instructions]")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])

    def test_broken_python_in_pack_is_invalid(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "bad.py", "def (:\n")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])
        self.assertTrue(any("pack_python_compiles" in e for e in report["errors"]))

    def test_bad_json_in_pack_is_invalid(self):
        self.write_skill("demo", pack=True)
        (self.root / "demo" / "config.json").write_text("{not json")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertFalse(report["valid"])

    def test_missing_tests_is_a_warning_not_an_error(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "run.py", "print('hi')\n")
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(any("pack_tests" in w for w in report["warnings"]))

    def test_world_readable_env_warns(self):
        self.write_skill("demo", pack=True)
        env = self.root / "demo" / ".env"
        env.write_text("SECRET=1\n")
        os.chmod(env, 0o644)
        report = tooling.validate_skill(self.root / "demo.md", user_dir=self.root)
        self.assertTrue(any("pack_env_permissions" in w for w in report["warnings"]))


class StaticTests(Base):
    def test_compile_python_ok_and_bad(self):
        good = self.root / "good.py"
        good.write_text("x = 1\n")
        self.assertEqual(tooling.compile_python(good)[0], True)
        bad = self.root / "bad.py"
        bad.write_text("def (:\n")
        ok, err = tooling.compile_python(bad)
        self.assertFalse(ok)
        self.assertIn("line", err)

    def test_check_shell(self):
        good = self.root / "good.sh"
        good.write_text("#!/usr/bin/env bash\nset -euo pipefail\ntrue\n")
        self.assertTrue(tooling.check_shell(good)[0])
        bad = self.root / "bad.sh"
        bad.write_text("if [ -f x\n")
        self.assertFalse(tooling.check_shell(bad)[0])

    def test_parse_structured(self):
        j = self.root / "a.json"
        j.write_text('{"a": 1}')
        self.assertTrue(tooling.parse_structured(j)[0])
        j.write_text("{nope")
        self.assertFalse(tooling.parse_structured(j)[0])
        y = self.root / "a.yaml"
        y.write_text("a: 1\n")
        self.assertTrue(tooling.parse_structured(y)[0])


CHECKER = (
    "import argparse, json, sys\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--check', action='store_true')\n"
    "p.add_argument('--json', action='store_true')\n"
    "a = p.parse_args()\n"
    "print(json.dumps({'status': 'ok'}) if a.json else 'ok')\n"
    "sys.exit(0)\n"
)
PASSING_TEST = (
    "def test_ok():\n"
    "    assert 1 + 1 == 2\n"
)


class TestRunnerTests(Base):
    def _good_pack(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "checker.py", CHECKER)
        tests = self.root / "demo" / "tests"
        tests.mkdir()
        (tests / "test_demo.py").write_text(PASSING_TEST)
        return self.root / "demo.md"

    def test_pass_for_good_pack(self):
        md = self._good_pack()
        result = tooling.run_skill_tests(md, user_dir=self.root)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["unit"]["status"], "PASS")
        self.assertEqual(result["functional"]["status"], "PASS")
        self.assertEqual(result["integration"]["status"], "PASS")

    def test_functional_failure_on_nonzero_exit(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "checker.py",
                          "import sys\n# --check\nsys.exit(3)\n")
        result = tooling.run_skill_tests(self.root / "demo.md", user_dir=self.root)
        self.assertEqual(result["functional"]["status"], "FAIL")
        self.assertEqual(result["status"], "FAIL")

    def test_json_without_status_fails(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "checker.py",
                          "import json\n# --check --json\nprint(json.dumps({'x': 1}))\n")
        result = tooling.run_skill_tests(self.root / "demo.md", user_dir=self.root)
        self.assertEqual(result["functional"]["status"], "FAIL")

    def test_validation_failure_short_circuits(self):
        self.write_skill("demo", description="")
        result = tooling.run_skill_tests(self.root / "demo.md", user_dir=self.root)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["unit"]["status"], "SKIP")
        self.assertEqual(result["functional"]["status"], "SKIP")

    def test_missing_tests_skips_unit(self):
        self.write_skill("demo", pack=True)
        result = tooling.run_skill_tests(self.root / "demo.md", user_dir=self.root)
        self.assertEqual(result["unit"]["status"], "SKIP")

    def test_integration_fails_on_unknown_tool(self):
        self.write_skill("demo", tools=["ghost_tool"])
        result = tooling.run_skill_tests(self.root / "demo.md",
                                         registry=_registry("web_search"),
                                         user_dir=self.root)
        # validation already fails on the unknown tool
        self.assertEqual(result["status"], "FAIL")


class ScaffoldTests(Base):
    def test_create_then_refuse_overwrite(self):
        out = self.root / "skills"
        first = tooling.create_skill_pack("made-here", "A made skill for tests.", out_dir=out)
        self.assertTrue(first["ok"], first)
        self.assertTrue((out / "made-here.md").exists())
        self.assertTrue((out / "made-here" / "tests").is_dir())
        second = tooling.create_skill_pack("made-here", "Again.", out_dir=out)
        self.assertFalse(second["ok"])
        self.assertIn("already exists", second["error"])
        third = tooling.create_skill_pack("made-here", "Again.", out_dir=out, overwrite=True)
        self.assertTrue(third["ok"])

    def test_bad_name_is_rejected(self):
        result = tooling.create_skill_pack("bad name", "x", out_dir=self.root)
        self.assertFalse(result["ok"])

    def test_system_only_makes_no_pack(self):
        out = self.root / "skills"
        result = tooling.create_skill_pack("sys-only", "System only skill.", out_dir=out,
                                           system_only=True)
        self.assertTrue(result["ok"])
        self.assertFalse((out / "sys-only").exists())


class ReportTests(Base):
    def test_ready_report_for_complete_pack(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "checker.py", CHECKER)
        tests = self.root / "demo" / "tests"
        tests.mkdir()
        (tests / "test_demo.py").write_text(PASSING_TEST)
        report = tooling.skill_report(self.root / "demo.md", user_dir=self.root)
        self.assertEqual(report["status"], "READY", report)
        self.assertGreaterEqual(report["completeness"]["score"], 90)

    def test_security_finding_is_surfaced(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "risky.py",
                          "import subprocess\n# --check\nsubprocess.run('ls', shell=True)\n")
        report = tooling.skill_report(self.root / "demo.md", user_dir=self.root)
        self.assertTrue(report["security"])
        self.assertTrue(any("shell=True" in f["issue"] for f in report["security"]))

    def test_discover_skills(self):
        self.write_skill("one")
        self.write_skill("two")
        found = tooling.discover_skills(self.root)
        self.assertEqual(len(found), 2)


class CliTests(Base):
    def _capture(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tooling.main(argv)
        return rc, buf.getvalue()

    def test_validate_cli_exit_codes(self):
        self.write_skill("demo")
        rc, out = self._capture(["validate", str(self.root / "demo.md")])
        self.assertEqual(rc, 0, out)
        self.assertIn("SKILL VALID", out)

        self.write_skill("broken", description="")
        rc, out = self._capture(["validate", str(self.root / "broken.md")])
        self.assertEqual(rc, 1)
        self.assertIn("SKILL INVALID", out)

    def test_test_cli_exit_code(self):
        self.write_skill("demo", pack=True)
        self.write_script("demo", "checker.py", CHECKER)
        rc, out = self._capture(["test", str(self.root / "demo.md")])
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: PASS", out)

    def test_wrapper_scripts_exist_and_are_executable(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        for name in ("create_skill.py", "validate_skill.py", "test_skill.py",
                     "test_all_skills.py", "skill_report.py"):
            path = scripts / name
            self.assertTrue(path.exists(), name)
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR, name)


class ToolIntegrationTests(unittest.TestCase):
    """The agent-facing tools that wrap the engine (validate/test/report)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = (config.DATA_DIR, config.ENV_FILE)
        config.DATA_DIR = Path(self.tmp.name)
        config.ENV_FILE = str(Path(self.tmp.name) / "backend.env")
        self.addCleanup(self._restore)

        registry = ToolRegistry()
        built = build_core_tools(registry, config)
        for tool in built:
            registry.register(tool)
        self.tools = {t.name: t for t in built}
        self.ctx = ToolContext()

    def _restore(self):
        config.DATA_DIR, config.ENV_FILE = self._orig

    def test_validate_test_report_roundtrip(self):
        out = self.tools["create_skill"].call(
            {"name": "disk-demo", "description": "Inspect disk usage and report it.",
             "system_prompt": "Run df -h and summarise it for the user.",
             "tools": ["web_search"]}, self.ctx)
        self.assertIn("created", out.lower())
        for filename, content in (
            ("checker.py", CHECKER),
            ("tests/test_disk_demo.py", PASSING_TEST),
            ("README.md", "# disk-demo\n\nInspects disk usage.\n"),
        ):
            self.assertIn(
                filename.split("/")[-1],
                self.tools["create_script"].call(
                    {"skill": "disk-demo", "filename": filename,
                     "content": content}, self.ctx))

        valid = self.tools["validate_skill"].call({"skill": "disk-demo"}, self.ctx)
        self.assertIn("VALID", valid)
        self.assertNotIn("INVALID", valid)

        tested = self.tools["test_skill"].call({"skill": "disk-demo"}, self.ctx)
        self.assertIn("RESULT: PASS", tested)

        report = self.tools["skill_report"].call({"skill": "disk-demo"}, self.ctx)
        self.assertIn("Completeness:", report)
        self.assertIn("Status: READY", report)

    def test_validate_tool_rejects_unknown_tool(self):
        self.tools["create_skill"].call(
            {"name": "bad-tool", "description": "Uses a tool that does not exist.",
             "system_prompt": "x", "tools": ["web_search"]}, self.ctx)
        # Overwrite with an unknown tool, bypassing create_skill's own guard.
        _path = Path(config.DATA_DIR) / "skills" / "bad-tool.md"
        _path.write_text(
            "---\nname: bad-tool\ndescription: Uses a ghost tool.\ntools: ghost_tool\n---\n\nbody\n")
        out = self.tools["validate_skill"].call({"skill": "bad-tool"}, self.ctx)
        self.assertIn("INVALID", out)
        self.assertIn("ghost_tool", out)


if __name__ == "__main__":
    unittest.main()
