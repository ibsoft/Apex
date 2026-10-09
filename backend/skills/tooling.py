"""Skill tooling: validate, test, report and scaffold APEX skills.

This is the engine behind the ``skill_creator`` skill's validation/repair loop.
The agent-facing tools (``validate_skill``, ``test_skill``, ``skill_report`` in
``tools/core_tools.py``) and the ``scripts/`` CLIs are thin wrappers over the
pure functions here, so the rules live in one place and are unit-tested.

A skill is a markdown file ``<name>.md`` with YAML frontmatter (see
``skills/manager.py``); an optional pack directory ``<name>/`` holds helper
scripts, tests and a 0600 ``.env``. Validation never *executes* a skill; testing
does, but only its ``--check``/test entry points.

Nothing here raises out of the public functions: every failure is returned as a
structured dict so a caller can repair and retry.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

SKILL_NAME_RE = re.compile(r"^[\w-]+$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_TODO_RE = re.compile(r"\[TODO:", re.I)

#: Extensions that are "executable behaviour" and therefore need a test.
_EXEC_SUFFIXES = {".py", ".sh", ".bash", ".js"}
_DATA_SUFFIXES = {".json", ".yaml", ".yml"}


class ToolingError(Exception):
    """A target could not be resolved or read."""


# --------------------------------------------------------------------------- #
# target resolution / parsing
# --------------------------------------------------------------------------- #
def _default_user_dir() -> Path:
    from config import config
    return Path(config.DATA_DIR) / "skills"


def _default_builtin_dir() -> Path:
    return Path(__file__).resolve().parent / "definitions"


def resolve_target(
    target: str | Path,
    *,
    user_dir: Path | None = None,
    builtin_dir: Path | None = None,
) -> tuple[Path, Path | None]:
    """Return ``(skill_md, pack_dir_or_None)`` for a path or a bare skill name.

    Accepts a ``<name>.md`` file, a pack directory that either contains its
    ``<name>.md`` or sits next to it, or a bare skill name resolved against the
    user skills dir first and the builtin definitions second.
    """
    user_dir = Path(user_dir) if user_dir else _default_user_dir()
    builtin_dir = Path(builtin_dir) if builtin_dir else _default_builtin_dir()
    raw = str(target).strip()
    if not raw:
        raise ToolingError("empty target")

    p = Path(raw).expanduser()
    if p.exists() and p.is_file():
        if p.suffix != ".md":
            raise ToolingError(f"not a skill markdown file: {p}")
        return p, _pack_for_md(p)
    if p.exists() and p.is_dir():
        inside = p / "SKILL.md"
        if not inside.exists():
            inside = p / f"{p.name}.md"
        if not inside.exists():
            candidates = sorted(p.glob("*.md"))
            if len(candidates) == 1:
                inside = candidates[0]
        if inside.exists():
            return inside, p
        # APEX layout: the pack dir is a sibling of <name>.md.
        sibling = p.parent / f"{p.name}.md"
        if sibling.exists():
            return sibling, p
        raise ToolingError(f"no skill markdown found in directory: {p}")

    if SKILL_NAME_RE.match(raw):
        for directory in (user_dir, builtin_dir):
            candidate = directory / f"{raw}.md"
            if candidate.exists():
                return candidate, _pack_for_md(candidate, directory)
        raise ToolingError(f"no skill named `{raw}` under {user_dir} or {builtin_dir}")

    raise ToolingError(f"cannot resolve skill target: {target}")


def _pack_for_md(md_path: Path, base: Path | None = None) -> Path | None:
    candidate = (base or md_path.parent) / md_path.stem
    return candidate if candidate.is_dir() else None


def parse_frontmatter(md_path: Path) -> tuple[dict, str, str]:
    """Return ``(meta, body, raw)``. Raises :class:`ToolingError` if malformed."""
    try:
        raw = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolingError(f"cannot read {md_path}: {exc}") from exc
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ToolingError(
            "missing YAML frontmatter: the file must start with a `---` line and "
            "close it with another `---` line"
        )
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise ToolingError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ToolingError("frontmatter must be a YAML mapping")
    return meta, m.group(2).strip(), raw


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _tool_names(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, (list, tuple)):
        return [str(t).strip() for t in value if str(t).strip()]
    return []


def validate_skill(
    target: str | Path,
    *,
    registry=None,
    user_dir: Path | None = None,
    builtin_dir: Path | None = None,
) -> dict:
    """Statically validate a skill. Never executes it; returns a report dict."""
    report: dict[str, Any] = {
        "target": str(target),
        "skill": "",
        "valid": False,
        "checks": [],
        "errors": [],
        "warnings": [],
    }

    def check(name: str, ok: bool, detail: str = "", *, warn: bool = False) -> bool:
        status = "pass" if ok else ("warn" if warn else "fail")
        report["checks"].append({"name": name, "ok": bool(ok), "status": status,
                                 "detail": detail})
        if not ok:
            bucket = report["warnings"] if warn else report["errors"]
            bucket.append(f"{name}: {detail}" if detail else name)
        return bool(ok)

    try:
        md_path, pack_dir = resolve_target(target, user_dir=user_dir,
                                           builtin_dir=builtin_dir)
    except ToolingError as exc:
        check("target", False, str(exc))
        return report

    report["skill"] = md_path.stem
    report["file"] = str(md_path)
    report["pack"] = str(pack_dir) if pack_dir else ""
    check("skill_file", md_path.is_file(), f"{md_path}")

    try:
        meta, body, raw = parse_frontmatter(md_path)
        check("frontmatter", True)
    except ToolingError as exc:
        check("frontmatter", False, str(exc))
        return report

    # name
    name = str(meta.get("name") or "").strip()
    if not name:
        name = md_path.stem
        check("name", True, f"absent; using filename `{name}`", warn=True)
    else:
        ok = SKILL_NAME_RE.match(name) is not None
        check("name", ok, name if ok else f"`{name}` must be letters, numbers, hyphens, underscores")
        check("name_length", len(name) <= MAX_NAME_LENGTH, f"{len(name)} chars")
    if name and name != md_path.stem:
        check("name_matches_file", False,
              f"name `{name}` != filename `{md_path.stem}`", warn=True)
    else:
        check("name_matches_file", True)

    # description
    description = str(meta.get("description") or "").strip()
    check("description", bool(description), "description is required")
    if description:
        check("description_todo", not _TODO_RE.search(description),
              "description contains an unfinished TODO")
        check("description_length", len(description) <= MAX_DESCRIPTION_LENGTH,
              f"{len(description)} chars")
        check("description_specific", len(description) >= 15,
              "description is too short to select this skill reliably", warn=True)

    # system prompt
    check("system_prompt", bool(body), "system prompt body is empty")
    if body:
        check("system_prompt_todo", not _TODO_RE.search(body),
              "system prompt contains an unfinished TODO placeholder")
    check("single_frontmatter", raw.count("\n---") <= 1,
          "a `---` line inside the prompt can close the frontmatter early",
          warn=True)

    # tools
    requested = _tool_names(meta.get("tools"))
    excluded = _tool_names(meta.get("exclude_tools"))
    if registry is not None:
        unknown = [t for t in requested + excluded if t != "ALL"
                   and registry.get(t) is None]
        check("tools_known", not unknown,
              "unknown tool(s): " + ", ".join(sorted(set(unknown))) if unknown else "")
        disabled = [t for t in requested if t != "ALL"
                    and registry.get(t) is not None and not registry.get(t).enabled]
        check("tools_enabled", not disabled,
              "disabled tool(s): " + ", ".join(sorted(set(disabled))) if disabled else "",
              warn=True)
    else:
        check("tools_known", True, "no registry supplied; skipped", warn=False)

    # pack
    if pack_dir is not None:
        _validate_pack(pack_dir, name or md_path.stem, check)
    else:
        check("pack", True, "no pack directory (definition-only skill)")

    report["valid"] = not report["errors"]
    return report


def _validate_pack(pack_dir: Path, name: str, check) -> None:
    check("pack", pack_dir.is_dir(), str(pack_dir))

    env = pack_dir / ".env"
    if env.exists():
        mode = stat.S_IMODE(env.stat().st_mode)
        check("pack_env_permissions", mode & 0o077 == 0,
              f"{env} is mode {oct(mode)}; secrets must be 0600", warn=True)

    scripts = [p for p in pack_dir.rglob("*")
               if p.is_file() and p.suffix.lower() in _EXEC_SUFFIXES]
    data_files = [p for p in pack_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in _DATA_SUFFIXES]

    compile_errors: list[str] = []
    shell_errors: list[str] = []
    for script in scripts:
        if script.suffix.lower() == ".py":
            ok, err = compile_python(script)
            if not ok:
                compile_errors.append(f"{script.name}: {err}")
        elif script.suffix.lower() in {".sh", ".bash"}:
            ok, err = check_shell(script)
            if not ok:
                shell_errors.append(f"{script.name}: {err}")
    if scripts:
        check("pack_python_compiles", not compile_errors,
              "; ".join(compile_errors))
        check("pack_shell_syntax", not shell_errors, "; ".join(shell_errors))

    data_errors: list[str] = []
    for data in data_files:
        ok, err = parse_structured(data)
        if not ok:
            data_errors.append(f"{data.name}: {err}")
    if data_files:
        check("pack_data_parses", not data_errors, "; ".join(data_errors))

    if scripts:
        tests = list((pack_dir / "tests").glob("test_*.py")) if (pack_dir / "tests").is_dir() else []
        check("pack_tests", bool(tests),
              f"executable code in {name}/ but no tests/test_*.py", warn=True)


# --------------------------------------------------------------------------- #
# static helpers
# --------------------------------------------------------------------------- #
def compile_python(path: Path) -> tuple[bool, str]:
    """Syntax-check a Python file without importing or running it."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"
    return True, ""


def check_shell(path: Path) -> tuple[bool, str]:
    import shutil
    bash = shutil.which("bash")
    if not bash:
        return True, "bash not installed; skipped"
    try:
        proc = subprocess.run([bash, "-n", str(path)], capture_output=True,
                              text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode:
        return False, (proc.stderr or proc.stdout).strip()
    return True, ""


def parse_structured(path: Path) -> tuple[bool, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    try:
        if path.suffix.lower() == ".json":
            json.loads(text)
        else:
            yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        return False, str(exc)
    return True, ""


# --------------------------------------------------------------------------- #
# execution: unit + functional + integration
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], *, timeout: int, cwd: Path | None = None) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=str(cwd) if cwd else None)
    except FileNotFoundError as exc:
        return {"rc": 127, "stdout": "", "stderr": str(exc), "blocked": True}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": f"timed out after {timeout}s",
                "blocked": True}
    except OSError as exc:
        return {"rc": 126, "stdout": "", "stderr": str(exc), "blocked": False}
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "blocked": False}


def _has_flag(path: Path, flag: str) -> bool:
    try:
        return flag in path.read_text(encoding="utf-8")
    except OSError:
        return False


def run_skill_tests(
    target: str | Path,
    *,
    registry=None,
    user_dir: Path | None = None,
    builtin_dir: Path | None = None,
    timeout: int = 120,
) -> dict:
    """Validate, then run unit, functional and integration checks."""
    result: dict[str, Any] = {
        "target": str(target),
        "validation": None,
        "compilation": None,
        "unit": None,
        "functional": None,
        "integration": None,
        "counts": {"passed": 0, "failed": 0, "skipped": 0, "blocked": 0},
        "status": "FAIL",
        "detail": "",
    }

    def tally(status: str) -> None:
        key = {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped",
               "BLOCKED": "blocked"}.get(status, "failed")
        result["counts"][key] += 1

    validation = validate_skill(target, registry=registry, user_dir=user_dir,
                                builtin_dir=builtin_dir)
    result["validation"] = validation
    result["item"] = validation.get("skill") or ""
    if not validation["valid"]:
        result["status"] = "FAIL"
        result["detail"] = "validation failed: " + "; ".join(validation["errors"])
        for key in ("compilation", "unit", "functional", "integration"):
            result[key] = {"status": "SKIP", "detail": "not run: validation failed"}
            tally("SKIP")
        return result

    # Compilation mirrors the validation checks (already run statically).
    compile_checks = [c for c in validation["checks"]
                      if c["name"] in {"pack_python_compiles", "pack_shell_syntax",
                                       "pack_data_parses"}]
    bad = [c for c in compile_checks if c["status"] == "fail"]
    if compile_checks:
        result["compilation"] = {"status": "FAIL" if bad else "PASS",
                                 "detail": "; ".join(c["detail"] for c in bad)}
    else:
        result["compilation"] = {"status": "PASS", "detail": "no static assets"}
    tally(result["compilation"]["status"])

    pack_dir = Path(validation["pack"]) if validation.get("pack") else None

    # Unit tests.
    tests_dir = (pack_dir / "tests") if pack_dir else None
    test_files = sorted(tests_dir.glob("test_*.py")) if tests_dir and tests_dir.is_dir() else []
    if test_files:
        proc = _run([sys.executable, "-m", "pytest", "-q", str(tests_dir)],
                    timeout=timeout, cwd=pack_dir)
        status = "BLOCKED" if proc["blocked"] else ("PASS" if proc["rc"] == 0 else "FAIL")
        result["unit"] = {"status": status, "detail": (proc["stdout"] + proc["stderr"]).strip()[-2000:],
                          "rc": proc["rc"]}
    else:
        has_code = bool(pack_dir and any(p.is_file() and p.suffix.lower() in _EXEC_SUFFIXES
                                         for p in pack_dir.rglob("*")))
        result["unit"] = {"status": "SKIP",
                          "detail": "no tests/ directory" + (" (executable code present)" if has_code else "")}
    tally(result["unit"]["status"])

    # Functional: run each script's --check (or --help) entry point.
    func_items = []
    scripts = sorted(p for p in (pack_dir.rglob("*") if pack_dir else [])
                     if p.is_file() and p.suffix.lower() in _EXEC_SUFFIXES
                     and "tests" not in p.parts)
    for script in scripts:
        if script.suffix.lower() == ".py":
            runner = [sys.executable, str(script)]
        elif script.suffix.lower() in {".sh", ".bash"}:
            runner = ["bash", str(script)]
        else:
            runner = ["node", str(script)]
        if _has_flag(script, "--check"):
            argv = runner + ["--check"] + (["--json"] if _has_flag(script, "--json") else [])
        elif _has_flag(script, "--help"):
            argv = runner + ["--help"]
        else:
            func_items.append({"script": script.name, "status": "SKIP",
                               "detail": "no --check/--help entry point"})
            continue
        proc = _run(argv, timeout=timeout, cwd=pack_dir)
        status = "BLOCKED" if proc["blocked"] else ("PASS" if proc["rc"] == 0 else "FAIL")
        detail = (proc["stdout"] + proc["stderr"]).strip()[-1000:]
        if status == "PASS" and _has_flag(script, "--json"):
            try:
                payload = json.loads(proc["stdout"])
                if isinstance(payload, dict) and "status" not in payload:
                    status, detail = "FAIL", "--json output has no `status` field"
                elif isinstance(payload, dict) and payload.get("status") == "error":
                    status, detail = "FAIL", detail or "reported status=error"
            except json.JSONDecodeError:
                status, detail = "FAIL", "--json output is not valid JSON"
        func_items.append({"script": script.name, "status": status, "detail": detail})
    if func_items:
        result["functional"] = {"status": _worst([i["status"] for i in func_items]),
                                "items": func_items}
    else:
        result["functional"] = {"status": "SKIP", "detail": "no runnable scripts"}
    tally(result["functional"]["status"])

    # Integration: APEX can parse and resolve the skill's tools.
    result["integration"] = _integration(md_path=Path(validation["file"]),
                                         user_dir=user_dir, registry=registry)
    tally(result["integration"]["status"])

    statuses = [result[k]["status"] for k in ("compilation", "unit", "functional",
                                              "integration")]
    if "FAIL" in statuses:
        result["status"] = "FAIL"
    elif "BLOCKED" in statuses:
        result["status"] = "BLOCKED"
    else:
        result["status"] = "PASS"
    result["detail"] = result["detail"] or _result_detail(result)
    return result


def _worst(statuses: list[str]) -> str:
    rank = {"FAIL": 0, "BLOCKED": 1, "SKIP": 2, "PASS": 3}
    return min(statuses, key=lambda s: rank.get(s, 0)) if statuses else "SKIP"


def _integration(*, md_path: Path, user_dir=None, registry=None) -> dict:
    try:
        from skills.manager import SkillManager
        mgr = SkillManager(user_dir if user_dir else md_path.parent)
        skill = mgr._parse(md_path)
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "FAIL", "detail": f"SkillManager could not parse: {exc}"}
    if skill is None:
        return {"status": "FAIL", "detail": "SkillManager refused the skill file"}
    if not skill.description or not skill.system_prompt:
        return {"status": "FAIL", "detail": "parsed skill has no description/prompt"}
    if registry is not None:
        unknown = [t for t in skill.tools if registry.get(t) is None]
        if unknown:
            return {"status": "FAIL",
                    "detail": "tools do not resolve: " + ", ".join(unknown)}
    return {"status": "PASS",
            "detail": f"loaded `{skill.name}` ({len(skill.tools) or 'all'} tools)"}


def _result_detail(result: dict) -> str:
    bits = []
    for key in ("compilation", "unit", "functional", "integration"):
        item = result.get(key) or {}
        if item.get("status") not in (None, "PASS"):
            bits.append(f"{key}={item.get('status')}")
    return ", ".join(bits) if bits else "all checks passed"


# --------------------------------------------------------------------------- #
# reporting / completeness
# --------------------------------------------------------------------------- #
def _security_scan(pack_dir: Path | None, prompt: str) -> list[dict]:
    if pack_dir is None:
        return []
    patterns = [
        (r"shell\s*=\s*True", "shell=True subprocess (prefer list arguments)"),
        (r"os\.system\s*\(", "os.system call"),
        (r"subprocess\.(?:run|call|Popen|check_output)\s*\(\s*[\"']", "subprocess with a string command"),
        (r"\beval\s*\(", "eval() on dynamic input"),
        (r"\bexec\s*\(", "exec() on dynamic input"),
        (r"pickle\.loads?\s*\(", "unsafe pickle deserialization"),
        (r"verify\s*=\s*False", "TLS verification disabled"),
        (r"chmod\s*\([^)]*0o?777|chmod\s+777", "world-writable permissions"),
    ]
    findings = []
    for script in sorted(pack_dir.rglob("*")):
        if not script.is_file() or script.suffix.lower() not in _EXEC_SUFFIXES:
            continue
        try:
            text = script.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern, label in patterns:
            if re.search(pattern, text):
                findings.append({"file": script.name, "issue": label})
    return findings


def skill_report(target: str | Path, *, registry=None, user_dir=None,
                 builtin_dir=None, timeout: int = 120) -> dict:
    validation = validate_skill(target, registry=registry, user_dir=user_dir,
                                builtin_dir=builtin_dir)
    try:
        md_path, pack_dir = resolve_target(target, user_dir=user_dir,
                                           builtin_dir=builtin_dir)
    except ToolingError:
        md_path, pack_dir, = None, None
    meta, body = {}, ""
    if md_path:
        try:
            meta, body, _ = parse_frontmatter(md_path)
        except ToolingError:
            pass

    files = []
    if pack_dir:
        files = sorted(str(p.relative_to(pack_dir)) for p in pack_dir.rglob("*")
                       if p.is_file())

    deps = []
    req = (pack_dir / "requirements.txt") if pack_dir else None
    if req and req.exists():
        deps = [ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    security = _security_scan(pack_dir, body)
    tests = run_skill_tests(target, registry=registry, user_dir=user_dir,
                            builtin_dir=builtin_dir, timeout=timeout) \
        if validation["valid"] else None

    report = {
        "skill": validation.get("skill", ""),
        "description": str(meta.get("description") or "").strip(),
        "prompt": body,
        "file": str(md_path) if md_path else "",
        "pack": str(pack_dir) if pack_dir else "",
        "files": files,
        "dependencies": deps,
        "validation": validation,
        "tests": tests,
        "security": security,
    }
    score, breakdown = _completeness(report)
    report["completeness"] = {"score": score, "breakdown": breakdown}
    report["status"] = (
        "READY" if score >= 90 and validation["valid"]
        and tests is not None and tests["status"] == "PASS"
        else ("BLOCKED" if tests is not None and tests["status"] == "BLOCKED"
              else "FAILED")
    )
    return report


def _completeness(report: dict) -> tuple[int, dict]:
    b: dict[str, int] = {}
    validation = report["validation"]
    tests = report.get("tests")
    pack = Path(report["pack"]) if report.get("pack") else None
    scripts = [p for p in pack.rglob("*") if p.is_file()
               and p.suffix.lower() in _EXEC_SUFFIXES and "tests" not in p.parts] if pack else []

    # architecture (15): valid name + frontmatter + resolvable tools
    arch = 15 if validation["valid"] else max(0, 15 - 3 * len(validation["errors"]))
    b["architecture_conformity"] = arch

    # definition/documentation (10)
    desc = report.get("description", "")
    b["skill_definition"] = (5 if desc and len(desc) >= 15 else 2) + \
                            (5 if len(validation["checks"]) and 8 <= len(desc) <= 1000 else 1)

    # core implementation (20): a definition-only skill earns it via the prompt
    if scripts:
        good = sum(1 for p in scripts if compile_python(p)[0])
        b["core_implementation"] = round(20 * good / len(scripts))
    else:
        b["core_implementation"] = 20 if validation["valid"] else 0

    # input validation (10): argparse / --check / explicit validation
    if scripts:
        good = sum(1 for p in scripts
                   if "argparse" in p.read_text(encoding="utf-8", errors="ignore")
                   or "--check" in p.read_text(encoding="utf-8", errors="ignore"))
        b["input_validation"] = round(10 * good / len(scripts))
    else:
        b["input_validation"] = 10

    # error handling (10)
    if scripts:
        good = sum(1 for p in scripts
                   if re.search(r"try\s*:|sys\.exit|except\s", p.read_text(encoding="utf-8", errors="ignore")))
        b["error_handling"] = round(10 * good / len(scripts))
    else:
        b["error_handling"] = 10

    # tests (15)
    unit = (tests or {}).get("unit") or {}
    b["automated_tests"] = {"PASS": 15, "SKIP": 4 if not scripts else 2,
                            "FAIL": 0, "BLOCKED": 0}.get(unit.get("status"), 0)

    # functional integration (10)
    func = (tests or {}).get("functional") or {}
    integ = (tests or {}).get("integration") or {}
    b["functional_integration"] = (5 if func.get("status") in {"PASS", "SKIP"} else 0) + \
                                  (5 if integ.get("status") == "PASS" else 0)

    # security (5)
    b["security_review"] = 5 if not report.get("security") else 2

    # documentation/examples (5)
    has_readme = bool(pack and (pack / "README.md").exists())
    has_examples = "example" in report.get("prompt", "").lower()
    b["documentation"] = (3 if has_readme else 1) + (2 if has_examples else 1)

    return min(100, sum(b.values())), b


# --------------------------------------------------------------------------- #
# scaffold
# --------------------------------------------------------------------------- #
_TEMPLATE_DIR = Path(__file__).resolve().parent / "creator_templates"


def _render(template: str, **values: Any) -> str:
    path = _TEMPLATE_DIR / template
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    return string.Template(text).safe_substitute(**values)


def create_skill_pack(
    name: str,
    description: str,
    *,
    out_dir: Path | None = None,
    tools: list[str] | None = None,
    system_prompt: str = "",
    system_only: bool = False,
    overwrite: bool = False,
    with_tests: bool = True,
) -> dict:
    """Scaffold ``<name>.md`` + pack directory. Returns a result dict."""
    name = (name or "").strip()
    description = " ".join((description or "").split())
    if not SKILL_NAME_RE.match(name):
        return {"ok": False, "error": "name must be letters, numbers, hyphens, underscores"}
    if not description:
        return {"ok": False, "error": "description is required"}
    out_dir = Path(out_dir) if out_dir else _default_user_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{name}.md"
    pack_dir = out_dir / name
    if md_path.exists() and not overwrite:
        return {"ok": False, "error": f"skill `{name}` already exists (use overwrite)"}
    if pack_dir.exists() and not overwrite:
        return {"ok": False, "error": f"pack directory {pack_dir} already exists"}

    prompt = system_prompt.strip() or _render("skill.md.tmpl", name=name, description=description)
    meta: dict[str, Any] = {"name": name, "description": description}
    if tools:
        meta["tools"] = ", ".join(tools)
    front = yaml.safe_dump(meta, sort_keys=False, width=1000, allow_unicode=True)
    md_path.write_text(f"---\n{front}---\n\n{prompt}\n", encoding="utf-8")

    created = [str(md_path)]
    if not system_only:
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "README.md").write_text(
            _render("README.md.tmpl", name=name, description=description),
            encoding="utf-8")
        (pack_dir / "requirements.txt").write_text("", encoding="utf-8")
        created += [str(pack_dir / "README.md"), str(pack_dir / "requirements.txt")]
        if with_tests:
            tests_dir = pack_dir / "tests"
            tests_dir.mkdir(exist_ok=True)
            test_path = tests_dir / f"test_{name.replace('-', '_')}.py"
            test_path.write_text(
                _render("test_skill.py.tmpl", name=name, module=name.replace("-", "_")),
                encoding="utf-8")
            created.append(str(test_path))

    from skills.manager import get_skill_manager
    get_skill_manager().refresh()
    return {"ok": True, "name": name, "file": str(md_path),
            "pack": "" if system_only else str(pack_dir), "created": created}


def discover_skills(root: str | Path | None = None) -> list[str]:
    root = Path(root) if root else _default_user_dir()
    if root.is_file() and root.suffix == ".md":
        return [str(root)]
    if not root.is_dir():
        return []
    if list(root.glob("*.md")):
        return sorted(str(p) for p in root.glob("*.md"))
    # A tree of pack directories.
    return sorted({str(p) for p in root.rglob("*.md")})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_validation(report: dict) -> None:
    for c in report["checks"]:
        tag = "PASS" if c["status"] == "pass" else c["status"].upper()
        line = f"[{tag}] {c['name']}"
        if c.get("detail") and c["status"] != "pass":
            line += f": {c['detail']}"
        print(line)
    print("SKILL VALID" if report["valid"] else "SKILL INVALID")
    for err in report["errors"]:
        print(f"  error: {err}")
    for warn in report["warnings"]:
        print(f"  warning: {warn}")


def _print_tests(result: dict) -> None:
    for key in ("validation", "compilation", "unit", "functional", "integration"):
        item = result.get(key)
        if key == "validation":
            status = "PASS" if item and item.get("valid") else "FAIL"
        else:
            status = (item or {}).get("status", "SKIP")
        print(f"{key.capitalize():<14} {status}")
        detail = (item or {}).get("detail") if isinstance(item, dict) else ""
        if detail and status not in {"PASS"}:
            for line in str(detail).splitlines():
                print(f"  {line}")
    counts = result["counts"]
    print(f"\nRESULT: {result['status']}  "
          f"(passed {counts['passed']}, failed {counts['failed']}, "
          f"skipped {counts['skipped']}, blocked {counts['blocked']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skill-tooling",
                                     description="Validate, test, report and scaffold APEX skills.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate"); p_val.add_argument("target"); p_val.add_argument("--json", action="store_true")
    p_test = sub.add_parser("test"); p_test.add_argument("target"); p_test.add_argument("--json", action="store_true")
    p_rep = sub.add_parser("report"); p_rep.add_argument("target"); p_rep.add_argument("--json", action="store_true")
    p_all = sub.add_parser("test-all"); p_all.add_argument("root", nargs="?"); p_all.add_argument("--json", action="store_true")
    p_new = sub.add_parser("create"); p_new.add_argument("--name", required=True)
    p_new.add_argument("--description", required=True); p_new.add_argument("--output", default=None)
    p_new.add_argument("--system-only", action="store_true"); p_new.add_argument("--force", action="store_true")
    p_new.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "validate":
        report = validate_skill(args.target)
        print(json.dumps(report, indent=2, ensure_ascii=False)) if args.json else _print_validation(report)
        return 0 if report["valid"] else 1
    if args.cmd == "test":
        result = run_skill_tests(args.target)
        print(json.dumps(result, indent=2, ensure_ascii=False)) if args.json else _print_tests(result)
        return 0 if result["status"] == "PASS" else 1
    if args.cmd == "report":
        report = skill_report(args.target)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"Skill: {report['skill']}")
            print(f"Completeness: {report['completeness']['score']}/100")
            print(f"Status: {report['status']}")
            for k, v in report["completeness"]["breakdown"].items():
                print(f"  {k}: {v}")
            for finding in report["security"]:
                print(f"  security: {finding['file']}: {finding['issue']}")
        return 0 if report["status"] == "READY" else 1
    if args.cmd == "test-all":
        targets = discover_skills(args.root)
        rows = []
        for t in targets:
            r = run_skill_tests(t)
            rows.append({"skill": t, "status": r["status"],
                         "validation": "PASS" if (r["validation"] or {}).get("valid") else "FAIL"})
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for row in rows:
                print(f"{row['validation']:<4} {row['status']:<7} {row['skill']}")
        return 0 if all(r["status"] == "PASS" for r in rows) else 1
    if args.cmd == "create":
        result = create_skill_pack(args.name, args.description, out_dir=args.output,
                                   system_only=args.system_only, overwrite=args.force)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result.get("error") if not result["ok"] else
                  f"Created skill `{result['name']}` at {result['file']}")
        return 0 if result["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
