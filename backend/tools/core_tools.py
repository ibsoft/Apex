"""Built-in tools that need no account: time, weather, web search/fetch,
safe calculator, sandboxed Python execution (opt-in)."""
from __future__ import annotations

import ast
import datetime as _dt
import html as _html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from ddgs import DDGS

from tools.base import Tool, ToolContext  # noqa: F401


# --------------------------------------------------------------------------- #
def _when() -> str:
    now = _dt.datetime.now()
    return f"{now.strftime('%A, %B %d %Y')} at {now.strftime('%H:%M:%S')} local time"


def _with_screen_link(ctx: ToolContext, cfg, command: str, result: str) -> str:
    """Save the full command output to a user-bound file and return a link the
    model must echo in its reply so the window manager opens it on the desktop."""
    if not ctx.user_id:
        return "Sign in before showing shell output on screen.\n\n" + result
    try:
        from tools.shell_out import save_shell_output
        url, filename = save_shell_output(cfg, ctx.user_id, command, result)
    except Exception as exc:
        return f"Could not capture shell output: {exc}\n\n{result}"
    if len(result) > 4000:
        preview = result[:4000] + "\n…"
    else:
        preview = result
    return (
        f"{preview}\n\n"
        f"Saved full output for the operator: [{filename}]({url}). "
        f"Include this link in your reply so it opens on the desktop."
    )


def build_core_tools(registry, cfg):
    """Registry is used only for dependency checks; returns a list of Tools."""

    def t_current_time(args, ctx: ToolContext):
        return f"The current date and time is {_when()}."

    def t_weather(args, ctx: ToolContext):
        city = (args.get("city") or "".strip())
        if not city:
            return "Please provide a city name."
        try:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
                timeout=15,
            ).json()
            hits = geo.get("results") or []
            if not hits:
                return f"No city found for `{city}`."
            loc = hits[0]
            lat, lon = loc["latitude"], loc["longitude"]
            label = f"{loc['name']}, {loc.get('admin1') or ''}, {loc.get('country') or ''}"
            wx = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                               "weather_code,wind_speed_10m,precipitation",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "forecast_days": 1,
                    "timezone": "auto",
                },
                timeout=15,
            ).json()
            cur = wx.get("current", {})
            daily = wx.get("daily", {})
            codes = {
                0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
                45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
                55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
                71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
                81: "showers", 82: "violent showers", 95: "thunderstorm",
                96: "thunderstorm w/ hail", 99: "severe thunderstorm",
            }
            desc = codes.get(cur.get("weather_code"), "unknown")
            today_max = (daily.get("temperature_2m_max") or [None])[0]
            today_min = (daily.get("temperature_2m_min") or [None])[0]
            return (
                f"Weather in {label}: {desc}, "
                f"{cur.get('temperature_2m', '?')}°C "
                f"(feels like {cur.get('apparent_temperature', '?')}°C), "
                f"humidity {cur.get('relative_humidity_2m', '?')}%, "
                f"wind {cur.get('wind_speed_10m', '?')} km/h. "
                f"Today: min {today_min}°C, max {today_max}°C."
            )
        except Exception as exc:
            return f"weather lookup failed: {exc}"

    def t_web_search(args, ctx: ToolContext):
        query = (args.get("query") or "").strip()
        n = min(int(args.get("max_results", 5) or 5), 10)
        if not query:
            return "Please provide a query."
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=n))
            if not results:
                return f"No results found for `{query}`."
            return json.dumps(results, ensure_ascii=False)[:4000]
        except Exception as exc:
            return f"web search failed: {exc}"

    def t_web_image_search(args, ctx: ToolContext):
        query = (args.get("query") or "").strip()
        n = min(int(args.get("max_results", 5) or 5), 10)
        if not query:
            return "Please provide a query."
        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(query, max_results=n))
            if not results:
                return f"No image results found for `{query}`."
            return json.dumps(results, ensure_ascii=False)[:4000]
        except Exception as exc:
            return f"web image search failed: {exc}"

    def t_web_news_search(args, ctx: ToolContext):
        query = (args.get("query") or "").strip()
        n = min(int(args.get("max_results", 5) or 5), 10)
        if not query:
            return "Please provide a query."
        try:
            with DDGS() as ddgs:
                results = list(ddgs.news(query, max_results=n))
            if not results:
                return f"No news results found for `{query}`."
            return json.dumps(results, ensure_ascii=False)[:4000]
        except Exception as exc:
            return f"web news search failed: {exc}"

    def t_web_fetch(args, ctx: ToolContext):
        url = (args.get("url") or "").strip()
        if not url:
            return "Please provide a URL."
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return f"Invalid URL: {url}"
        if int(args.get("max_chars", 6000)) > 20000:
            return "max_chars must be <= 20000"
        try:
            resp = requests.get(url, timeout=20,
                                headers={"User-Agent": "Mozilla/5.0 APEX-assistant/1.0"})
            resp.raise_for_status()
            text = _html_to_text(resp.text)
            if (args.get("title_only") or False):
                m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.I | re.S)
                return f"<title>{m.group(1).strip()}</title>" if m else "no <title>"
            return text[: int(args.get("max_chars", 6000))]
        except Exception as exc:
            return f"web fetch failed: {exc}"

    def t_calculate(args, ctx: ToolContext):
        expr = (args.get("expression") or "").strip()
        if not expr or len(expr) > 500:
            return "Provide a math expression, e.g. '(2**10) * 3.5 / 7'"
        try:
            node = ast.parse(expr, mode="eval").body
            allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                       ast.Call, ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult,
                       ast.Div, ast.Pow, ast.Mod, ast.FloorDiv, ast.USub, ast.UAdd,
                       ast.Pow, ast.Compare)
            if not isinstance(node, allowed):
                return "expression uses disallowed syntax"
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"abs", "round", "min", "max", "sum", "sqrt"}:
                    pass
                else:
                    return "expression calls disallowed function" if not (
                        isinstance(node.func, ast.Name) and node.func.id == "abs"
                    ) else None
            scope = {
                "__builtins__": {},
                "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
            }
            try:
                scope["sqrt"] = lambda x: x ** 0.5
            except Exception:
                pass
            result = eval(compile(node, "<expr>", "eval"), scope)  # noqa: S307
            return f"{expr} = {result}"
        except Exception as exc:
            return f"calculation failed: {exc}"

    def t_run_python(args, ctx: ToolContext):
        code = (args.get("code") or "").strip()
        if not code:
            return "Provide Python code."
        if not cfg.ENABLE_RUN_PYTHON:
            return "The run_python tool is disabled (set ENABLE_RUN_PYTHON=true)."
        with tempfile.TemporaryDirectory(prefix="apex-py-") as td:
            env = {"PATH": "/usr/bin:/bin"}
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True, text=True, timeout=30,
                    cwd=td, env=env,
                )
            except subprocess.TimeoutExpired:
                return "python execution timed out after 30s"
            out = (proc.stdout or "")[-4000:]
            err = (proc.stderr or "")[-1200:]
            if proc.returncode != 0:
                return f"exit {proc.returncode}\nstderr:\n{err}\nstdout:\n{out}"
            return out or "ok"

    def t_run_shell(args, ctx: ToolContext):
        command = (args.get("command") or "").strip()
        if not command:
            return "Provide a shell command."
        if not cfg.ENABLE_RUN_SHELL:
            return "The run_shell tool is disabled (set ENABLE_RUN_SHELL=true)."
        timeout = getattr(cfg, "RUN_SHELL_TIMEOUT", 60)
        on_screen = bool(args.get("on_screen"))
        if args.get("sudo"):
            from tools.vapt_tools import SUDO_MARKER, _redact_password, _run_shell, sudocred_get
            cred = sudocred_get(ctx.user_id)
            if not cred:
                return f"{SUDO_MARKER}This command needs sudo and no session credential is stored — the password popup will appear."
            rc, out, err, _dur = _run_shell(command, timeout, True, cfg, ctx.user_id)
            out = _redact_password(out, cred["password"])
            err = _redact_password(err, cred["password"])
            if rc == 98:
                return (f"{SUDO_MARKER}The sudo credential was rejected or expired — the password popup will appear.\n"
                        f"stderr:\n{err or 'password required'}")
            if rc != 0:
                result = f"exit {rc}\nstderr:\n{err}\nstdout:\n{out}"
            else:
                result = out or "ok"
        else:
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=os.getcwd(),
                    env=os.environ,
                )
            except subprocess.TimeoutExpired:
                return f"shell execution timed out after {timeout}s"
            out = proc.stdout or ""
            err = proc.stderr or ""
            if proc.returncode != 0:
                result = f"exit {proc.returncode}\nstderr:\n{err}\nstdout:\n{out}"
            else:
                result = out or "ok"
        if ctx.output_destination == "notepad":
            from tools.notepad_tools import notepad_output_result
            return notepad_output_result(result)
        if on_screen:
            return _with_screen_link(ctx, cfg, command, result)
        # Truncate what the model has to read; the on-screen file keeps it whole.
        output = result[-4000:]
        if len(result) > 4000:
            output = f"[output truncated]\n{output}"
        return output

    def t_list_tools(args, ctx: ToolContext):
        all_tools = registry.all() if hasattr(registry, "all") else []
        rows = [
            {"name": t.name,
             "description": (t.description or "")[:500],
             "dangerous": bool(t.dangerous),
             "enabled": bool(t.enabled)}
            for t in sorted(all_tools, key=lambda t: t.name)
        ]
        return json.dumps(rows, ensure_ascii=False)

    def t_create_skill(args, ctx: ToolContext):
        name = (args.get("name") or "").strip()
        description = " ".join((args.get("description") or "").split())
        system_prompt = (args.get("system_prompt") or "").strip()
        tools = args.get("tools") or []
        model = (args.get("model") or "").strip()
        overwrite = bool(args.get("overwrite", False))
        require_tool = bool(args.get("require_tool", False))
        exclude = args.get("exclude_tools") or []
        if not name or not description or not system_prompt:
            return "name, description, and system_prompt are required."
        if not re.match(r"^[\w-]+$", name):
            return "Skill name must contain only letters, numbers, hyphens, and underscores."
        if isinstance(tools, str):
            requested = [t.strip() for t in tools.split(",") if t.strip()]
        elif isinstance(tools, list):
            requested = [str(t).strip() for t in tools if str(t).strip()]
        else:
            return "tools must be a list of tool names, a comma-separated string, or [\"ALL\"]."
        if isinstance(exclude, str):
            exclude = [t.strip() for t in exclude.split(",") if t.strip()]
        elif isinstance(exclude, list):
            exclude = [str(t).strip() for t in exclude if str(t).strip()]
        else:
            return "exclude_tools must be a list of tool names."
        # A name that does not exist would make the skill silently unable to do
        # the one thing it was created for: the engine offers only registered
        # tools, so an unknown name is dropped without a word. Reject here where
        # the model can still see the error and retry.
        unknown = [t for t in requested + exclude if t != "ALL" and registry.get(t) is None]
        if unknown:
            return (
                "Unknown tool(s): " + ", ".join(sorted(set(unknown))) + ". "
                "Call list_tools to see the exact names, then retry. A skill "
                "listing a tool that does not exist can never use it."
            )
        disabled = [t for t in requested
                    if t != "ALL" and registry.get(t) is not None and not registry.get(t).enabled]
        skills_dir = Path(cfg.DATA_DIR) / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        file_path = skills_dir / f"{name}.md"
        if file_path.exists() and not overwrite:
            return f"Skill `{name}` already exists. Set overwrite=true to replace it."

        # A line of exactly three dashes closes the frontmatter for the reader
        # (skills/manager._parse), so a markdown horizontal rule inside the
        # prompt would cut the skill in half. `***` renders identically.
        system_prompt = re.sub(r"(?m)^-{3}[ \t]*$", "***", system_prompt)

        # yaml.safe_dump, not hand-built lines: a description containing ": "
        # (or a leading "*") would otherwise produce frontmatter that
        # safe_load rejects, and the skill would be created but never load.
        meta: dict = {"name": name, "description": description}
        if requested:
            meta["tools"] = ", ".join(requested)
        if model:
            meta["model"] = model
        if require_tool:
            meta["require_tool"] = True
        if exclude:
            meta["exclude_tools"] = ", ".join(exclude)
        front = yaml.safe_dump(meta, sort_keys=False, width=1000, allow_unicode=True)
        content = f"---\n{front}---\n\n" + system_prompt + "\n"
        file_path.write_text(content, encoding="utf-8")

        # Pick up the new skill immediately.
        from skills.manager import get_skill_manager
        get_skill_manager().refresh()
        msg = f"Skill `{name}` created at {file_path}."
        builtin = Path(__file__).resolve().parents[1] / "skills" / "definitions" / f"{name}.md"
        if builtin.exists():
            msg += (f" Note: user skills shadow built-ins of the same name - "
                    f"`{name}` now replaces the built-in skill.")
        if disabled:
            msg += (f" Warning: currently disabled (feature flag off): "
                    f"{', '.join(disabled)}. The skill cannot use it until enabled.")
        msg += (f" Pack directory for scripts and the skill's .env: "
                f"{skills_dir / name}/ (tools: create_script, set_env).")
        return msg

    def t_create_script(args, ctx: ToolContext):
        skill = (args.get("skill") or "").strip()
        filename = (args.get("filename") or "").strip()
        content = args.get("content")
        overwrite = bool(args.get("overwrite", False))
        if not re.match(r"^[\w-]+$", skill):
            return "create_script error: skill must be a valid skill name (letters, numbers, hyphens, underscores)."
        skills_dir = Path(cfg.DATA_DIR) / "skills"
        if not (skills_dir / f"{skill}.md").exists():
            return (f"create_script error: no skill named `{skill}` - create the skill "
                    f"first with create_skill.")
        if not filename or "\\" in filename or filename.startswith("/"):
            return "create_script error: filename must be a relative path with forward slashes, e.g. fetch.py."
        if not isinstance(content, str) or not content.strip():
            return "create_script error: content is required."
        if len(content) > 200_000:
            return "create_script error: content too large (200,000 character limit)."
        base = (skills_dir / skill).resolve()
        target = (base / filename).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return "create_script error: filename escapes the skill's directory."
        if target == base:
            return "create_script error: filename must name a file."
        if any(part == ".env" for part in Path(filename).parts):
            return ("create_script error: the skill's .env is written by set_env "
                    "(scope=skill), so it gets the right permissions.")
        if target.exists() and not overwrite:
            return f"create_script error: {target.name} already exists. Set overwrite=true to replace it."
        suffix = target.suffix.lower()
        # Only files that can actually run get +x; a .md or .json stays 0600.
        executable = bool(args.get("executable", True)) and suffix in {".py", ".sh", ".bash", ".js"}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
            fd = os.open(target, flags, 0o700)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.chmod(target, 0o700 if executable else 0o600)
        except FileExistsError:
            return f"create_script error: {target.name} already exists. Set overwrite=true to replace it."
        except OSError as exc:
            return f"create_script error: {exc}"
        runner = {".py": "python3", ".sh": "bash", ".bash": "bash", ".js": "node"}.get(suffix)
        run = f"{runner} {target}" if runner else str(target)
        return (f"Script written to {target} "
                f"({'executable' if executable else 'data file'}, {len(content)} chars).\n"
                f"Run it from the skill with terminal_command: {run}")

    def t_set_env(args, ctx: ToolContext):
        key = (args.get("key") or "").strip()
        scope = (args.get("scope") or "skill").strip().lower()
        skill = (args.get("skill") or "").strip()
        remove = bool(args.get("remove", False))
        value = args.get("value")
        if not re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", key):
            return "set_env error: key must be UPPER_SNAKE_CASE, e.g. MY_API_TOKEN."
        if scope not in {"skill", "backend"}:
            return "set_env error: scope must be 'skill' or 'backend'."
        if remove:
            if value not in (None, ""):
                return "set_env error: pass either value or remove=true, not both."
        else:
            if not isinstance(value, str) or not value:
                # An empty value in .env would pin "" and override the default
                # forever - the same trap the SIP mirror documents. Deleting the
                # line is the way to say "no value".
                return ("set_env error: value must be a non-empty string. To clear a key, "
                        "pass remove=true, which deletes its line.")
            if len(value) > 4096:
                return "set_env error: value too long (4,096 character limit)."

        if scope == "skill":
            if not re.match(r"^[\w-]+$", skill):
                return "set_env error: skill is required for scope=skill (letters, numbers, hyphens, underscores)."
            skills_dir = Path(cfg.DATA_DIR) / "skills"
            if not (skills_dir / f"{skill}.md").exists():
                return f"set_env error: no skill named `{skill}` - create the skill first."
            env_path = skills_dir / skill / ".env"
            where = f"the skill env file {env_path}"
        else:
            env_path = Path(getattr(cfg, "ENV_FILE", "") or
                            Path(__file__).resolve().parents[1] / ".env")
            where = f"the backend env file {env_path}"

        if remove and not env_path.exists():
            return f"set_env: {key} was not set (no file at {env_path} yet)."

        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            from tools.envfile import upsert_env
            upsert_env(env_path, {key: "" if remove else value})
        except OSError as exc:
            return f"set_env error: could not write {env_path}: {exc}"

        notes = []
        if scope == "backend":
            # Checked BEFORE the live apply below: setattr would make hasattr
            # true and the unknown-key warning could never fire.
            known = hasattr(cfg, key)
            example = env_path.with_name(".env.example")
            documented = False
            try:
                documented = example.exists() and bool(
                    re.search(rf"(?m)^{re.escape(key)}=", example.read_text(encoding="utf-8")))
            except OSError:
                pass
            if remove:
                os.environ.pop(key, None)
                if hasattr(cfg, key):
                    # Best effort: "" reads as unset in the codebase's
                    # `attr or fallback` pattern. A restart lands on the true
                    # default, which is why the reply says so.
                    setattr(cfg, key, "")
                notes.append("cleared from this process; restart the backend for "
                             "modules that read it at import time")
            else:
                os.environ[key] = value
                setattr(cfg, key, value)
                notes.append("applied live; a restart is still needed for modules "
                             "that read it at import time")
            if not known and not documented:
                notes.append(f"warning: {key} is not a known config variable "
                             f"(absent from config and .env.example) - only scripts "
                             f"reading it via os.getenv will see it")

        action = "removed" if remove else "set"
        msg = f"{key} {action} in {where}."
        if notes:
            # First character only: str.capitalize() would lowercase the whole
            # rest of the sentence, key names included.
            joined = "; ".join(notes)
            msg += " " + joined[0].upper() + joined[1:] + "."
        return msg

    def _skill_target(args) -> str:
        return (args.get("skill") or args.get("path") or "").strip()

    def t_validate_skill(args, ctx: ToolContext):
        target = _skill_target(args)
        if not target:
            return "validate_skill error: `skill` is required (a skill name or a path to <name>.md)."
        from skills import tooling
        report = tooling.validate_skill(target, registry=registry)
        head = "VALID" if report["valid"] else "INVALID"
        lines = [f"{head}: {report.get('skill') or target}"]
        for c in report["checks"]:
            if c["status"] == "pass":
                continue
            lines.append(f"[{c['status'].upper()}] {c['name']}: {c['detail']}")
        if report["valid"] and not report["warnings"]:
            lines.append("All structural checks passed. Next: test_skill.")
        return "\n".join(lines)

    def t_test_skill(args, ctx: ToolContext):
        target = _skill_target(args)
        if not target:
            return "test_skill error: `skill` is required (a skill name or a path to <name>.md)."
        from skills import tooling
        result = tooling.run_skill_tests(target, registry=registry)
        lines = []
        for key in ("validation", "compilation", "unit", "functional", "integration"):
            item = result.get(key)
            if key == "validation":
                status = "PASS" if item and item.get("valid") else "FAIL"
                detail = "" if status == "PASS" else "; ".join((item or {}).get("errors", []))
            else:
                status = (item or {}).get("status", "SKIP")
                detail = (item or {}).get("detail", "")
            lines.append(f"{key.capitalize():<14} {status}")
            if detail and status not in ("PASS", "SKIP"):
                lines.append(f"  {str(detail)[:600]}")
        counts = result["counts"]
        lines.append("")
        lines.append(f"RESULT: {result['status']} (passed {counts['passed']}, "
                     f"failed {counts['failed']}, skipped {counts['skipped']}, "
                     f"blocked {counts['blocked']})")
        if result["status"] != "PASS":
            lines.append("Fix the FAIL/BLOCKED items above, then call test_skill again. "
                         "Do not report success until RESULT is PASS.")
        return "\n".join(lines)

    def t_skill_report(args, ctx: ToolContext):
        target = _skill_target(args)
        if not target:
            return "skill_report error: `skill` is required."
        from skills import tooling
        report = tooling.skill_report(target, registry=registry)
        lines = [
            f"Skill: {report['skill']}",
            f"Completeness: {report['completeness']['score']}/100",
            f"Status: {report['status']}",
        ]
        for key, val in report["completeness"]["breakdown"].items():
            lines.append(f"  {key}: {val}")
        if report["validation"]["errors"]:
            lines.append("Validation errors:")
            lines += [f"  - {e}" for e in report["validation"]["errors"]]
        if report["security"]:
            lines.append("Security findings (review, do not ignore):")
            lines += [f"  - {f['file']}: {f['issue']}" for f in report["security"]]
        if report["dependencies"]:
            lines.append("Dependencies: " + ", ".join(report["dependencies"]))
        return "\n".join(lines)

    return [
        Tool("current_time",
             "Get the current local date and time.",
             {"type": "object", "properties": {}},
             t_current_time),
        Tool("get_weather",
             "Get current weather for a city.",
             {"type": "object",
              "properties": {"city": {"type": "string", "description": "City name, e.g. Berlin"}},
              "required": ["city"]},
             t_weather),
        Tool("web_search",
             "Search the web (DuckDuckGo) and return top results as JSON.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "max_results": {"type": "integer", "default": 5},
              }, "required": ["query"]},
             t_web_search),
        Tool("web_image_search",
             "Search the web for images (DuckDuckGo) and return direct image URLs that can be displayed to the user.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string", "description": "Image search query"},
                  "max_results": {"type": "integer", "default": 5},
              }, "required": ["query"]},
             t_web_image_search),
        Tool("web_news_search",
             "Search the web for news (DuckDuckGo) and return news titles, snippets, source URLs and image URLs as JSON.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string", "description": "News search query"},
                  "max_results": {"type": "integer", "default": 5},
              }, "required": ["query"]},
             t_web_news_search),
        Tool("web_fetch",
             "Fetch a URL and return its readable text content.",
             {"type": "object",
              "properties": {
                  "url": {"type": "string"},
                  "max_chars": {"type": "integer", "default": 6000},
                  "title_only": {"type": "boolean", "default": False},
              }, "required": ["url"]},
             t_web_fetch),
        Tool("calculate",
             "Safely evaluate a math expression.",
             {"type": "object",
              "properties": {"expression": {"type": "string"}},
              "required": ["expression"]},
             t_calculate),
        Tool("run_python",
             "Execute Python code in a sandboxed subprocess and return stdout.",
             {"type": "object",
              "properties": {"code": {"type": "string"}},
              "required": ["code"]},
             t_run_python, dangerous=True),
        Tool("run_shell",
             "Run a local shell command on the host (e.g. ls, ping, nmap, ss, ip, ifconfig, netstat, journalctl, apt). Requires ENABLE_RUN_SHELL=true. Set on_screen=true when the operator wants the output displayed on the main desktop window, then include the returned [name](url) link in your reply unchanged.",
             {"type": "object",
              "properties": {
                  "command": {"type": "string", "description": "Shell command to execute verbatim."},
                  "sudo": {"type": "boolean", "default": False,
                           "description": "Run with elevation via sudo -S; if no session credential is stored the sudo password popup appears."},
                  "on_screen": {"type": "boolean", "default": False,
                                "description": "Save the full command output to a file the operator can view and download in a desktop window. Use when asked to show/display the output on the screen. Always echo the returned link in your reply."},
              },
              "required": ["command"]},
             t_run_shell, dangerous=True),
        Tool("list_tools",
             "List every tool the model can use: exact name, description, dangerous "
             "and enabled flags. Call this before choosing a skill's `tools` list, "
             "so every name written into the skill actually exists.",
             {"type": "object", "properties": {}},
             t_list_tools),
        Tool("create_skill",
             "Create a new Apex skill by writing a markdown definition file into "
             "DATA_DIR/skills/. Names in `tools` are validated against the live "
             "tool list (use list_tools first). Returns the skill's pack directory "
             "for follow-up create_script / set_env calls.",
             {"type": "object",
              "properties": {
                  "name": {"type": "string", "description": "Short skill name (letters, numbers, hyphens, underscores)."},
                  "description": {"type": "string", "description": "One-line description of what the skill does."},
                  "system_prompt": {"type": "string", "description": "The system prompt that defines the skill's behavior."},
                  "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names the skill may use, e.g. ['web_search', 'web_fetch'] or ['ALL']. Every name must exist (see list_tools)."},
                  "model": {"type": "string", "description": "Optional model override for this skill."},
                  "require_tool": {"type": "boolean", "default": False, "description": "Force the first model call of every turn to actually call a tool - for skills whose contract is 'do it', not 'explain it'."},
                  "exclude_tools": {"type": "array", "items": {"type": "string"}, "description": "Tools this skill must never be offered, even with tools ALL (e.g. hide headless run_shell from a skill that promises visible commands)."},
                  "overwrite": {"type": "boolean", "default": False, "description": "Replace the skill file if it already exists."},
              },
              "required": ["name", "description", "system_prompt"]},
             t_create_skill, dangerous=True),
        Tool("create_script",
             "Write a helper script into a skill's pack directory "
             "(DATA_DIR/skills/<skill>/). The skill must already exist. The skill "
             "runs the script through terminal_command; returns the command to use. "
             "Only .py/.sh/.bash/.js get +x (mode 0700, everything else 0600). The "
             "skill's .env cannot be written here - use set_env for it.",
             {"type": "object",
              "properties": {
                  "skill": {"type": "string", "description": "Name of the already-created skill this script belongs to."},
                  "filename": {"type": "string", "description": "Path relative to the skill's directory, e.g. fetch.py or bin/run.sh. No leading slash, no .."},
                  "content": {"type": "string", "description": "Full script source. Start with a shebang; read the skill's .env from a file next to the script."},
                  "executable": {"type": "boolean", "default": True, "description": "Mark runnable (.py/.sh/.bash/.js) scripts executable."},
                  "overwrite": {"type": "boolean", "default": False, "description": "Replace the script if it already exists."},
              },
              "required": ["skill", "filename", "content"],
              "additionalProperties": False},
             t_create_script, dangerous=True),
        Tool("set_env",
             "Set or remove an environment variable for a skill pack. scope=skill "
             "(default) writes DATA_DIR/skills/<skill>/.env, which the skill's own "
             "scripts read - use this for API keys the scripts need. scope=backend "
             "writes the backend .env (the file config.py loads) and applies the "
             "value live; use it only when a built-in backend tool needs the "
             "variable. Values are never echoed back. An empty result means the "
             "key was deleted (remove=true), never that it was set to empty.",
             {"type": "object",
              "properties": {
                  "key": {"type": "string", "description": "UPPER_SNAKE_CASE variable name, e.g. WEATHER_API_KEY."},
                  "value": {"type": "string", "description": "New non-empty value. Required unless remove=true."},
                  "remove": {"type": "boolean", "default": False, "description": "Delete the key's line instead of setting it."},
                  "scope": {"type": "string", "enum": ["skill", "backend"], "default": "skill", "description": "skill = the skill's own .env (for its scripts); backend = the backend config env file."},
                  "skill": {"type": "string", "description": "Skill name. Required when scope=skill."},
              },
              "required": ["key"],
              "additionalProperties": False},
             t_set_env, dangerous=True),
        Tool("validate_skill",
             "Statically validate an Apex skill: frontmatter, name, description, "
             "prompt, tool names (against the live registry), and every file in its "
             "pack directory (Python compiles, shell syntax, JSON/YAML parse). Never "
             "executes the skill, so it is always safe to call. Call it after every "
             "create_skill / create_script / set_env change.",
             {"type": "object",
              "properties": {
                  "skill": {"type": "string", "description": "Skill name (e.g. disk-monitor) or a path to its <name>.md file."},
              },
              "required": ["skill"],
              "additionalProperties": False},
             t_validate_skill),
        Tool("test_skill",
             "Validate a skill, then run its tests: unit tests in the pack's tests/ "
             "directory, the --check entry point of each helper script, and an "
             "integration check that the skill loads and its tools resolve. Returns "
             "PASS/FAIL/SKIP/BLOCKED per stage. Fix failures and re-run until PASS.",
             {"type": "object",
              "properties": {
                  "skill": {"type": "string", "description": "Skill name or a path to its <name>.md file."},
              },
              "required": ["skill"],
              "additionalProperties": False},
             t_test_skill, dangerous=True),
        Tool("skill_report",
             "Produce a completeness report (0-100) for a skill: validation, tests, "
             "files, dependencies, security findings and a READY/BLOCKED/FAILED "
             "verdict. Use it before declaring a generated skill finished.",
             {"type": "object",
              "properties": {
                  "skill": {"type": "string", "description": "Skill name or a path to its <name>.md file."},
              },
              "required": ["skill"],
              "additionalProperties": False},
             t_skill_report, dangerous=True),
    ]


# --------------------------------------------------------------------------- #
def _parse_ddg(html_text: str, n: int) -> list[dict]:
    results = []
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.S
    ):
        url, title = _html.unescape(m.group(1)), re.sub(r"<.*?>", "", m.group(2))
        snippet = ""
        nxt = html_text[m.end():]
        sm = re.search(r'class="result__snippet".*?>(.*?)</a>', nxt, re.S)
        if sm:
            snippet = _html.unescape(re.sub(r"<.*?>", "", sm.group(1))).strip()
        results.append({
            "title": _html.unescape(title).strip(),
            "url": _html.unescape(url),
            "snippet": _html.unescape(snippet),
        })
        if len(results) >= n:
            break
    return results


def _html_to_text(html_text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?is)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()