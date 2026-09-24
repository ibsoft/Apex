"""CODE skill tools: developer project lifecycle, daily progress journals and
GitHub push.

Every coding project is a git repository under ``config.CODE_PROJECTS``:

    <CODE_PROJECTS>/<project>/
        README.md, .gitignore, scaffold files
        progress/          daily work logs (progress/YYYY-MM-DD.md)

``code_start`` asks for (and validates) a project name, runs ``git init`` and
makes an initial commit. ``code_projects`` / ``code_use`` / ``code_destroy``
manage the inventory. ``code_progress`` keeps the daily journal that drives the
skill's "propose actions / ask questions / surface new features" behaviour.

GitHub pushes are optional. The first ``code_push`` requires a Personal Access
Token: when none is stored the tool returns an ``APEX_GITHUB::`` marker which
the chat translator converts into a ``github_token`` stream event so the frontend
can show its pasted-token popup. The token is validated against the API, stored
per-user chmod-600 under ``config.CODE_GITHUB_DIR`` (gitignored), and is never
written into git config, remotes or commit messages.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests
from flask import jsonify, request

from tools.base import Tool, ToolContext

GITHUB_MARKER = "APEX_GITHUB::"

_GITIGNORE = "\n".join([
    "__pycache__/", "*.py[cod]", ".venv/", "venv/", ".env", "env/",
    "node_modules/", ".next/", "dist/", "build/", "bin/", "obj/", "*.user",
    ".vs/", ".idea/", ".vscode/", "*.db.sqlite3", ".DS_Store", "",
])

_README = "# {project}\n\nDevelopment project scaffolded by APEX.\nSee `progress/` for the daily work log.\n"

_SKELETONS: dict[str, dict[str, str]] = {
    "flask": {
        "app.py": "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route(\"/\")\ndef home():\n    return \"Hello from {project}\"\n\n\nif __name__ == \"__main__\":\n    app.run(debug=True)\n",
        "requirements.txt": "flask\n",
    },
    "python": {
        "main.py": "def main():\n    print(\"Hello from {project}\")\n\n\nif __name__ == \"__main__\":\n    main()\n",
        "requirements.txt": "",
    },
    "react": {
        "package.json": json.dumps({
            "name": "{slug}", "private": True, "version": "0.1.0", "type": "module",
            "scripts": {"dev": "vite", "build": "vite build"},
            "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
            "devDependencies": {"@vitejs/plugin-react": "^4.3.1", "vite": "^5.4.0"},
        }, indent=2) + "\n",
        "index.html": "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"/><title>{project}</title></head><body><div id=\"root\"></div><script type=\"module\" src=\"/src/main.jsx\"></script></body></html>\n",
        "src/main.jsx": "import React from 'react'\nimport { createRoot } from 'react-dom/client'\nimport App from './App.jsx'\n\ncreateRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)\n",
        "src/App.jsx": "export default function App() {\n  return <div style={{ fontFamily: 'sans-serif', padding: 40 }}>Hello from {project}</div>\n}\n",
    },
    "node": {
        "package.json": json.dumps({
            "name": "{slug}", "private": True, "version": "0.1.0", "main": "index.js",
            "scripts": {"start": "node index.js"},
        }, indent=2) + "\n",
        "index.js": "const http = require('http')\nconst port = process.env.PORT || 3000\n\nhttp.createServer((req, res) => {\n  res.end('Hello from {project}')\n}).listen(port, () => console.log('listening on', port))\n",
    },
    "csharp": {
        "{slug}.csproj": "<Project Sdk=\"Microsoft.NET.Sdk\">\n  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n",
        "Program.cs": "Console.WriteLine(\"Hello from {project}\");\n",
    },
    "web": {
        "index.html": "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"/><title>{project}</title><link rel=\"stylesheet\" href=\"style.css\"/></head><body><h1>Hello from {project}</h1><script src=\"script.js\"></script></body></html>\n",
        "style.css": "body { font-family: sans-serif; margin: 40px; }\n",
        "script.js": "console.log('Hello from {project}');\n",
    },
}

_PROGRESS_MARKERS = {
    "note": "- {ts} · {text}",
    "question": "- {ts} [?] {text}",
    "action": "- {ts} [>] {text}",
    "feature": "- {ts} [+] {text}",
}


def _projects_root(config) -> Path:
    if not getattr(config, "CODE_PROJECTS", ""):
        raise ValueError(
            "CODE_PROJECTS is not configured — set it in backend/.env (e.g. "
            "/home/you/Development/Projects) and restart the backend."
        )
    return Path(config.CODE_PROJECTS).expanduser().resolve()


def _project_dir(config, name: str):
    if not name or not str(name).strip():
        return None, "Provide a project name."
    root = _projects_root(config)
    slug = str(name).strip()
    if "/" in slug or "\\" in slug or slug in (".", ".."):
        return None, f"`{name}` is not a valid project name — use a short name like 'my-app'."
    candidate = (root / slug).resolve()
    if not candidate.is_relative_to(root):
        return None, f"Project `{slug}` escapes the projects root."
    return candidate, ""


def _git(args: list[str], cwd: Path, env: dict | None = None, timeout: int = 60):
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 99, "", "git timed out"
    except Exception as exc:
        return 97, "", f"could not run git: {exc}"


def _run(
    cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 180,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError:
        return 98, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 99, "", f"{cmd[0]} timed out"
    except Exception as exc:
        return 97, "", f"could not run {cmd[0]}: {exc}"


def _venv_python(proj: Path) -> Path:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    return proj / ".venv" / bin_dir / ("python.exe" if os.name == "nt" else "python")


def _ensure_venv(proj: Path) -> str:
    """Create a project-local virtual environment if missing. Returns guidance."""
    python = _venv_python(proj)
    if python.exists():
        return f"Project .venv already present (use `.venv/bin/python` / `.venv/bin/pip`)."
    rc, out, gout = _run(["python3", "-m", "venv", ".venv"], proj)
    if rc == 0 and python.exists():
        return f"Created project .venv (install packages with `.venv/bin/pip`, run with `.venv/bin/python`)."
    if rc == 98:
        return (
            "`python3` was not found — cannot scaffold the .venv yet. Install python3 "
            "with run_shell + sudo, then recreate the venv with `python3 -m venv .venv`."
        )
    return f".venv creation failed ({gout.strip() or out.strip() or rc}); recreate with `python3 -m venv .venv`."


def _is_repo(proj: Path) -> bool:
    return (proj / ".git").exists()


def _ensure_git_author(proj: Path, login: str = "") -> None:
    _rc, name, _ = _git(["config", "user.name"], proj)
    _rc, email, _ = _git(["config", "user.email"], proj)
    if not (name or "").strip():
        _git(["config", "user.name", login or "APEX Developer"], proj)
    if not (email or "").strip():
        fallback = f"{login}@users.noreply.github.com" if login else "apex@localhost"
        _git(["config", "user.email", fallback], proj)


def _branch_name(config) -> str:
    return getattr(config, "CODE_BRANCH", "main") or "main"


def _count_files(proj: Path) -> int:
    if not proj.is_dir():
        return 0
    return sum(1 for _ in proj.rglob("*") if _.is_file() and ".git" not in _.parts)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _progress_files(proj: Path) -> list[Path]:
    base = proj / "progress"
    if not base.is_dir():
        return []
    return sorted(base.glob("*.md"))


def _read_open_items(proj: Path) -> list[str]:
    items: list[str] = []
    for path in _progress_files(proj)[-14:]:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if any(m in line for m in ("[?]", "[>]", "[+]")):
                items.append(f"({path.stem}) {line}")
    return items


def _token_path(config, user_id: str) -> Path:
    directory = Path(config.CODE_GITHUB_DIR).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user_id}.json"


def github_validate(token: str) -> tuple[str | None, str]:
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
    except Exception as exc:
        return None, f"Could not reach the GitHub API: {exc}"
    if resp.status_code != 200:
        return None, f"GitHub rejected the token (HTTP {resp.status_code}): {resp.text[:300]}"
    login = (resp.json() or {}).get("login")
    return (login if login else "unknown"), ""


def github_token_get(config, user_id: str) -> dict | None:
    path = _token_path(config, user_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("token"):
            return None
        return data
    except Exception:
        return None


def github_token_set(config, user_id: str, token: str) -> tuple[str, str]:
    login, err = github_validate(token)
    if err:
        return "", err
    path = _token_path(config, user_id)
    path.write_text(json.dumps({"token": token, "login": login}), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return login, ""


def github_token_clear(config, user_id: str) -> None:
    path = _token_path(config, user_id)
    if path.exists():
        path.unlink()


def translate_github_marker(output: str) -> tuple[bool, str]:
    """Detect the APEX_GITHUB:: marker in a tool result for the SSE translator."""
    if not output or GITHUB_MARKER not in output:
        return False, ""
    idx = output.find(GITHUB_MARKER)
    reason = output[idx + len(GITHUB_MARKER):].strip() or "GitHub token required"
    return True, reason


def build_code_tools(config) -> list[Tool]:
    def _blocked(config) -> str:
        try:
            _projects_root(config)
            return ""
        except ValueError as exc:
            return str(exc)

    def t_start(args: dict, ctx: ToolContext) -> str:
        blocked = _blocked(config)
        if blocked:
            return blocked
        name = (args.get("project") or "").strip()
        if not name:
            projects = t_projects({"list": False}, ctx)
            return (
                "Provide a project name before starting.\n"
                "Say something like `start project my-app`.\n"
                + projects
            )
        proj, err = _project_dir(config, name)
        if err:
            return err
        root = _projects_root(config)
        root.mkdir(parents=True, exist_ok=True)
        existed = proj.exists()
        if existed:
            if _is_repo(proj):
                return (
                    f"Project `{name}` already exists as a git repository.\n"
                    f"{t_use({'project': name}, ctx)}"
                )
        else:
            proj.mkdir(parents=True)
        if not _is_repo(proj):
            branch = _branch_name(config)
            rc, out, gout = _git(["init", "-b", branch], proj)
            if rc != 0:
                return f"Could not git-init the project:\n{gout or out}\n{err}"
        stage = (args.get("stack") or args.get("template") or "").strip().lower()
        if not (proj / "README.md").exists():
            (proj / "README.md").write_text(_README.format(project=name), encoding="utf-8")
        if not (proj / ".gitignore").exists():
            (proj / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
        if stage and stage in _SKELETONS:
            for fname, content in _SKELETONS[stage].items():
                target = proj / fname.format(slug=re.sub(r"[^a-z0-9-]", "-", name.lower()), project=name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text(content, encoding="utf-8")
        venv_note = ""
        if stage in ("flask", "python"):
            venv_note = _ensure_venv(proj)
        _ensure_git_author(proj)
        rc, out, gout = _git(["add", "-A"], proj)
        rc, out, gout = _git(["commit", "-m", f"Initial scaffold: {name}"], proj)
        if rc not in (0, 1):
            return f"Initial commit failed:\n{gout or out}"
        result = (
            f"Project `{name}` ready at {proj} (git branch `{_branch_name(config)}`).\n"
            f"Stack: {stage or 'default'} · committed."
        )
        if venv_note:
            result += f"\n{venv_note}"
        result += "\nNow analyze the needs and plan the build."
        return result

    def t_projects(args: dict, ctx: ToolContext) -> str:
        blocked = _blocked(config)
        if blocked:
            return blocked
        root = _projects_root(config)
        if not root.is_dir():
            return "No code projects yet — start one with a project name."
        rows = []
        for proj in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if not proj.is_dir():
                continue
            if not _is_repo(proj):
                rows.append(f"- **{proj.name}** (not a git repository yet)")
                continue
            rc, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], proj)
            rc, head, _ = _git(["log", "-1", "--format=%h %ad %s", "--date=short"], proj)
            rc, stat, _ = _git(["status", "--porcelain"], proj)
            dirty = len([l for l in stat.splitlines() if l.strip()])
            today = (_today() + ".md") in {p.name for p in _progress_files(proj)}
            rows.append(
                f"- **{proj.name}** · {branch.strip() or '?'} · {head.strip() or 'no commits'} · "
                f"{dirty} uncommitted · {_count_files(proj)} files · progress {'today' if today else 'stale'}"
            )
        return f"Code projects ({len(rows)}):\n" + "\n".join(rows) if rows else "No code projects yet."

    def t_use(args: dict, ctx: ToolContext) -> str:
        blocked = _blocked(config)
        if blocked:
            return blocked
        name = (args.get("project") or "").strip()
        proj, err = _project_dir(config, name)
        if err:
            return err
        if not proj.exists():
            return f"No project `{name}` yet — start it first (code_start with a project name)."
        if not _is_repo(proj):
            return f"`{proj}` exists but is not a git repository — re-run code_start."
        rc, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], proj)
        rc, head, _ = _git(["log", "-1", "--format=%h %ad %s", "--date=short"], proj)
        rc, stat, _ = _git(["status", "--porcelain"], proj)
        dirty = [l for l in stat.splitlines() if l.strip()][:15]
        files = sorted(p.name for p in proj.iterdir() if p.is_file() and not p.name.startswith("."))
        today_file = proj / "progress" / f"{_today()}.md"
        today_log = today_file.read_text(encoding="utf-8", errors="replace") if today_file.is_file() else "(no progress logged today)"
        recent = [p.name for p in _progress_files(proj)[-5:-1]]
        open_items = _read_open_items(proj)
        lines = [
            f"Project `{name}` — {proj}",
            f"Branch {branch.strip() or '?'} · last commit {head.strip() or 'none'}",
            f"Uncommitted ({len(dirty)}):\n    " + "\n    ".join(dirty) if dirty else "Working tree clean",
            f"Top-level files: {', '.join(files) or '—'}",
            f"Today's progress:\n{today_log}",
            f"Recent logs: {', '.join(recent) or '—'}",
        ]
        if open_items:
            lines.append("Open questions / proposed actions / feature ideas:\n    " + "\n    ".join(open_items))
        lines.append("Analyze the state, then propose next actions or ask me the key decisions.")
        return "\n".join(lines)

    def t_destroy(args: dict, ctx: ToolContext) -> str:
        blocked = _blocked(config)
        if blocked:
            return blocked
        name = (args.get("project") or "").strip()
        confirm = bool(args.get("confirm"))
        proj, err = _project_dir(config, name)
        if err:
            return err
        if not proj.is_dir():
            return f"No project `{name}` — nothing to delete."
        items = _count_files(proj)
        if not confirm:
            return (
                f"Project `{name}` has {items} files and would be deleted permanently "
                f"(git history included). Set confirm=true to proceed."
            )
        shutil.rmtree(proj, ignore_errors=True)
        return f"Code project `{name}` deleted ({items} files removed)."

    def t_progress(args: dict, ctx: ToolContext) -> str:
        blocked = _blocked(config)
        if blocked:
            return blocked
        name = (args.get("project") or "").strip()
        proj, err = _project_dir(config, name)
        if err:
            return err
        if not proj.is_dir():
            return f"No project `{name}` yet — start it first."
        progress_dir = proj / "progress"
        progress_dir.mkdir(parents=True, exist_ok=True)
        entry = ""
        for key in ("note", "question", "action", "feature"):
            value = (args.get(key) or "").strip()
            if not value:
                continue
            entry += _PROGRESS_MARKERS[key].format(ts=time.strftime("%H:%M"), text=value) + "\n"
        if entry:
            today_file = progress_dir / f"{_today()}.md"
            header = f"# {_today()} — {name}\n\n" if not today_file.exists() else ""
            with today_file.open("a", encoding="utf-8") as fh:
                fh.write(header + entry)
            return (
                f"Logged to {today_file.name}.\n\n" + today_file.read_text(encoding="utf-8").strip()
            )
        today_file = progress_dir / f"{_today()}.md"
        body = today_file.read_text(encoding="utf-8", errors="replace").strip() if today_file.is_file() else "(nothing logged today)"
        open_items = _read_open_items(proj)
        result = [f"Progress for `{name}` today:\n{body}"]
        if open_items:
            result.append("Open items (answer or close them today):\n    " + "\n    ".join(open_items))
        return "\n\n".join(result)

    def t_github_status(args: dict, ctx: ToolContext) -> str:
        cred = github_token_get(config, ctx.user_id) if ctx.user_id else None
        if not cred:
            return (
                f"{GITHUB_MARKER}No GitHub token stored.\nTo enable pushes, create a Personal Access "
                f"Token (Settings → Developer settings → Tokens → repo scope) and the popup will "
                f"store it for future use."
            )
        return f"GitHub is connected as `{cred.get('login', '?')}`. Ready to push."

    def t_push(args: dict, ctx: ToolContext) -> str:
        blocked = _blocked(config)
        if blocked:
            return blocked
        if not ctx.user_id:
            return "Sign in before pushing to GitHub."
        name = (args.get("project") or "").strip()
        message = (args.get("message") or "").strip() or "Update"
        proj, err = _project_dir(config, name)
        if err:
            return err
        if not proj.is_dir():
            return f"No project `{name}` yet — start it first."
        if not _is_repo(proj):
            return f"`{proj}` is not a git repository yet."
        rc, out, gout = _git(["add", "-A"], proj)
        rc, out, gout = _git(["commit", "-m", message], proj)
        if rc not in (0, 1):
            return f"Commit failed:\n{gout or out}"
        if "nothing to commit" in (out + gout):
            committed = "already committed"
        else:
            committed = f"committed: {message}"
        cred = github_token_get(config, ctx.user_id)
        if not cred:
            return (
                f"{GITHUB_MARKER}No GitHub token stored — the GitHub token popup will appear. "
                f"Create a Personal Access Token (scope: repo) and paste it there, then say *continue*."
            )
        login = cred.get("login", "")
        token = cred["token"]
        slug = name
        api = "https://api.github.com"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            repo_check = requests.get(f"{api}/repos/{login}/{slug}", headers=headers, timeout=15)
        except Exception as exc:
            return f"Could not check the GitHub repo: {exc}"
        if repo_check.status_code == 404:
            payload = {
                "name": slug,
                "private": True,
                "description": f"{name} · developed with APEX",
                "auto_init": False,
            }
            try:
                created = requests.post(f"{api}/user/repos", headers=headers, json=payload, timeout=15)
            except Exception as exc:
                return f"Could not create the GitHub repo: {exc}"
            if created.status_code not in (200, 201, 422):
                return f"GitHub refused to create the repo (HTTP {created.status_code}): {created.text[:300]}"
        branch = _branch_name(config)
        rc, remote, _ = _git(["remote", "get-url", "origin"], proj)
        push_url = f"https://github.com/{login}/{slug}.git"
        if rc != 0 or not remote.strip():
            _git(["remote", "add", "origin", push_url], proj)
        helper = '!f() { echo "username=${APEX_GH_LOGIN}"; echo "password=${APEX_GH_TOKEN}"; }; f'
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "APEX_GH_LOGIN": login,
            "APEX_GH_TOKEN": token,
        }
        rc, out, gout = _git(
            ["-c", f"credential.helper={helper}", "push", "-u", push_url, f"HEAD:{branch}"],
            proj,
            env=env,
            timeout=180,
        )
        if rc != 0:
            return (
                f"Push failed (HTTP/Git): {gout or out}\n"
                f"Is the token still valid? Say *push* again once refreshed."
            )
        return (
            f"{committed} and pushed to https://github.com/{login}/{slug}.\n"
            f"Remote: {push_url} · branch `{branch}`.\n"
            f"{out.strip() or ''}"
        )

    schemas = {
        "project": {"type": "string", "description": "Project name (no slashes), e.g. my-app."},
    }

    return [
        Tool(
            "code_start",
            "Start (or list-for-choice) a coding project. Requires a project name; creates the folder as a git repository with an initial commit, README, and an optional scaffold.",
            {
                "type": "object",
                "properties": {
                    **schemas,
                    "stack": {
                        "type": "string",
                        "enum": ["flask", "python", "react", "node", "csharp", "web", ""],
                        "description": "Optional scaffold: flask (Python), react (Vite), node, csharp (.NET 8), web (static html/css/js).",
                    },
                },
                "required": ["project"],
            },
            t_start,
        ),
        Tool(
            "code_projects",
            "List all developer projects with git state (branch, last commit, uncommitted changes, progress recency) so work can be resumed.",
            {"type": "object", "properties": {}},
            t_projects,
        ),
        Tool(
            "code_use",
            "Open an existing project for work: git status, uncommitted changes, top-level files, today's progress and open questions/actions.",
            {
                "type": "object",
                "properties": dict(schemas),
                "required": ["project"],
            },
            t_use,
        ),
        Tool(
            "code_destroy",
            "Delete a developer project permanently (files and git history). Requires confirm=true.",
            {
                "type": "object",
                "properties": {
                    **schemas,
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set true to permanently delete the project.",
                    },
                },
                "required": ["project"],
            },
            t_destroy,
            dangerous=True,
        ),
        Tool(
            "code_progress",
            "Log or read the daily progress journal for a project. Add note/question/action/feature entries; with no entry it reads back today's log and pending items.",
            {
                "type": "object",
                "properties": {
                    **schemas,
                    "note": {"type": "string", "description": "What was done (timestamped entry)."},
                    "question": {"type": "string", "description": "Decision needed from the operator."},
                    "action": {"type": "string", "description": "Proposed next action."},
                    "feature": {"type": "string", "description": "Proposed new feature."},
                },
                "required": ["project"],
            },
            t_progress,
        ),
        Tool(
            "code_github_status",
            "Check whether a GitHub token is stored and who is connected (never returns the token).",
            {"type": "object", "properties": {}},
            t_github_status,
        ),
        Tool(
            "code_push",
            "Commit local changes and push to GitHub. Creates the repository automatically (private) when it does not exist. Shows the token popup when no token is stored.",
            {
                "type": "object",
                "properties": {
                    **schemas,
                    "message": {"type": "string", "description": "Commit message for this push."},
                },
                "required": ["project"],
            },
            t_push,
            dangerous=True,
        ),
    ]


def register_code_routes(app, require_user, config):
    """GitHub token popup endpoint + status."""

    @app.post("/api/code/github-token")
    def code_github_token_set():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in first."}), 401
        data = request.get_json(silent=True) or {}
        token = data.get("token")
        if not isinstance(token, str) or not token.strip():
            return jsonify({"error": "Token is required."}), 400
        login, err = github_token_set(config, str(user["id"]), token.strip())
        if err:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True, "login": login})

    @app.delete("/api/code/github-token")
    def code_github_token_clear():
        user = require_user()
        if user:
            github_token_clear(config, str(user["id"]))
        return jsonify({"ok": True})

    @app.get("/api/code/github/status")
    def code_github_status():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in first."}), 401
        cred = github_token_get(config, str(user["id"]))
        return jsonify(
            {
                "token": "available" if cred else "unavailable",
                "login": (cred or {}).get("login", ""),
            }
        )

    return app