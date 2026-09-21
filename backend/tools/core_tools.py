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
from ddgs import DDGS

from tools.base import Tool, ToolContext  # noqa: F401


# --------------------------------------------------------------------------- #
def _when() -> str:
    now = _dt.datetime.now()
    return f"{now.strftime('%A, %B %d %Y')} at {now.strftime('%H:%M:%S')} local time"


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
        out = (proc.stdout or "")[-4000:]
        err = (proc.stderr or "")[-1200:]
        if proc.returncode != 0:
            return f"exit {proc.returncode}\nstderr:\n{err}\nstdout:\n{out}"
        return out or "ok"

    def t_create_skill(args, ctx: ToolContext):
        name = (args.get("name") or "").strip()
        description = (args.get("description") or "").strip()
        system_prompt = (args.get("system_prompt") or "").strip()
        tools = args.get("tools") or []
        model = (args.get("model") or "").strip()
        overwrite = bool(args.get("overwrite", False))
        if not name or not description or not system_prompt:
            return "name, description, and system_prompt are required."
        if not re.match(r"^[\w-]+$", name):
            return "Skill name must contain only letters, numbers, hyphens, and underscores."
        skills_dir = Path(cfg.DATA_DIR) / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        file_path = skills_dir / f"{name}.md"
        if file_path.exists() and not overwrite:
            return f"Skill `{name}` already exists. Set overwrite=true to replace it."

        meta_lines = [f"name: {name}", f"description: {description}"]
        if tools:
            if isinstance(tools, str):
                meta_lines.append(f"tools: {tools}")
            elif isinstance(tools, list):
                if len(tools) == 1 and tools[0] == "ALL":
                    meta_lines.append("tools: ALL")
                else:
                    meta_lines.append("tools: " + ", ".join(str(t) for t in tools))
        if model:
            meta_lines.append(f"model: {model}")
        content = "---\n" + "\n".join(meta_lines) + "\n---\n\n" + system_prompt + "\n"
        file_path.write_text(content, encoding="utf-8")

        # Pick up the new skill immediately.
        from skills.manager import get_skill_manager
        get_skill_manager().refresh()
        return f"Skill `{name}` created at {file_path}."

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
             "Run a local shell command on the host (e.g. ping, nmap, ss, ip, ifconfig, netstat, journalctl). Requires ENABLE_RUN_SHELL=true.",
             {"type": "object",
              "properties": {
                  "command": {"type": "string", "description": "Shell command to execute verbatim."},
              },
              "required": ["command"]},
             t_run_shell, dangerous=True),
        Tool("create_skill",
             "Create a new Apex skill by writing a markdown definition file.",
             {"type": "object",
              "properties": {
                  "name": {"type": "string", "description": "Short skill name (letters, numbers, hyphens, underscores)."},
                  "description": {"type": "string", "description": "One-line description of what the skill does."},
                  "system_prompt": {"type": "string", "description": "The system prompt that defines the skill's behavior."},
                  "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names the skill may use, e.g. ['web_search', 'web_fetch'] or ['ALL']."},
                  "model": {"type": "string", "description": "Optional model override for this skill."},
                  "overwrite": {"type": "boolean", "default": False, "description": "Replace the skill file if it already exists."},
              },
              "required": ["name", "description", "system_prompt"]},
             t_create_skill, dangerous=True),
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