"""VAPT tools: an authorized vulnerability-assessment assistant.

Every target is a project under ``config.VAPT_PROJECTS``:

    <VAPT_PROJECTS>/<target>/
        artifacts/   raw command outputs + JSON metadata
        scripts/     generated batch scripts
        reports/     generated markdown reports (served via signed links)

Tools let the VAPT skill initialize a project, run commands or generated
script batches (optionally in parallel), read artifacts back for analysis,
and persist a professional markdown report with an expiring download link.

Commands that need root use ``sudo -S`` fed from a per-user session
credential store. The store lives only in server memory, is never written to
disk, is never logged, and is never returned to the model. When no credential
is available the tool returns an ``APEX_SUDO::<reason>`` marker which the chat
translator converts into a ``sudo_password`` stream event so the frontend can
show its centered password popup.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
import traceback
import uuid
from pathlib import Path

from flask import jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from public_urls import public_url
from tools.base import Tool, ToolContext

VAPT_SALT = "apex-vapt-download-v1"
SUDO_MARKER = "APEX_SUDO::"

# --------------------------------------------------------------------------- #
# Per-user session credential cache (memory only).
# --------------------------------------------------------------------------- #
#   _SUDO_STORE[user_id] = {"password": str, "saved": bool, "expires_at": float}
_SUDO_STORE: dict[str, dict] = {}


def sudocred_get(user_id: str, now: float | None = None) -> dict | None:
    rec = _SUDO_STORE.get(user_id)
    if not rec:
        return None
    if rec.get("expires_at", 0) and (now or time.time()) > rec["expires_at"]:
        _SUDO_STORE.pop(user_id, None)
        return None
    return dict(rec)


def sudocred_set(user_id: str, password: str, save: bool, config) -> None:
    ttl = 0.0 if save else float(getattr(config, "VAPT_SUDO_SINGLE_USE_SECONDS", 120))
    if save:
        ttl = float(getattr(config, "VAPT_SUDO_TTL_MINUTES", 0) * 60) or 0.0
    expires_at = (time.time() + ttl) if ttl > 0 else 0.0
    _SUDO_STORE[user_id] = {
        "password": str(password),
        "saved": bool(save),
        "expires_at": expires_at,
    }


def sudocred_clear(user_id: str) -> None:
    _SUDO_STORE.pop(user_id, None)


# --------------------------------------------------------------------------- #
# Path safety helpers (mirrors obsidian_tools._resolve).
# --------------------------------------------------------------------------- #
def _projects_root(config) -> Path:
    return Path(getattr(config, "VAPT_PROJECTS", "") or "_").expanduser().resolve()


def _project_dir(config, target: str):
    if not target or target.strip() in (".", ".."):
        return None, "Provide a target name."
    root = _projects_root(config)
    rel = Path(target).name.strip() or target
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        return None, f"Target `{target}` escapes the VAPT projects root."
    return candidate, ""


def _clean_target(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", target).strip("._")[:120] or "target"


def _ensure_project(config, target: str):
    proj, err = _project_dir(config, target)
    if err:
        return None, err
    for sub in ("artifacts", "scripts", "reports"):
        (proj / sub).mkdir(parents=True, exist_ok=True)
    manifest = proj / "project.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {"target": target, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                indent=2,
            ),
            encoding="utf-8",
        )
    return proj, ""


# --------------------------------------------------------------------------- #
# Command helpers
# --------------------------------------------------------------------------- #
def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


_SHELL_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "function", "in", "select", "time", "export", "local",
    "read", "set", "unset", "shift", "source", ".", "alias", "declare",
    "trap", "exec", "exit", "return", "wait", "printf", "echo",
}


def _check_available(command: str) -> list[str]:
    """Return the binaries the command depends on that are not installed.

    Only the *leading* token of each command segment is a candidate binary;
    arguments, flags, paths and redirections are never treated as binaries.
    """
    missing: list[str] = []
    for segment in re.split(r"&&|\|\||[|;>]+", command):
        first = segment.strip()
        if not first or first[0] in ('"', "'", "(", "<"):
            continue
        token = first.split()[0].strip("\"'")
        if not token or "/" in token or token.startswith(("-", "=")):
            continue
        if token in _SHELL_KEYWORDS:
            continue
        if not _has_binary(token) and token not in missing:
            missing.append(token)
    return missing


def _run_shell(command: str, timeout: int, use_sudo: bool, config, user_id: str) -> tuple[int, str, str, float]:
    """Run a command. Returns (rc, stdout, stderr, duration_seconds)."""
    if use_sudo:
        cred = sudocred_get(user_id)
        if not cred:
            return (98, "", "password required", 0.0)
        cmd: list[str] = ["sudo", "-S", "-p", "", "bash", "-c", command]
        inp = cred["password"] + "\n"
    else:
        cmd = ["bash", "-c", command]
        inp = None
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=inp,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return (99, "", f"timed out after {timeout}s", round(time.time() - started, 1))
    except Exception as exc:
        return (97, "", f"could not run command: {exc}", round(time.time() - started, 1))
    duration = round(time.time() - started, 1)
    out = proc.stdout or ""
    err = proc.stderr or ""
    if use_sudo and proc.returncode != 0 and "password" in err.lower():
        # Wrong/absent credential: forget it so the popup reappears next round.
        sudocred_clear(user_id)
        return (98, out, err, duration)
    return (proc.returncode, out, err, duration)


def _artifact_path(proj: Path, phase: str, tag: str, seq: int) -> Path:
    phase_dir = proj / "artifacts" / _clean_target(phase)
    phase_dir.mkdir(parents=True, exist_ok=True)
    return phase_dir / f"{seq:03d}_{_clean_target(tag) or 'output'}.out"


def _write_artifact(meta: dict, out: str, err: str, path: Path) -> None:
    path.write_text(out, encoding="utf-8")
    meta_path = path.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps(
            {**meta, "stdout_chars": len(out), "stderr_chars": len(err),
             "stderr_tail": err[-500:]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _redact_password(text: str, password: str) -> str:
    if password:
        text = text.replace(password, "••••••")
    return text


def _summarize_artifacts(proj: Path, phase: str = "", tag: str = "") -> list[str]:
    base = proj / "artifacts"
    if phase:
        base = base / _clean_target(phase)
    if not base.exists():
        return []
    files = sorted(base.glob("*.out")) if phase else sorted(base.rglob("*.out"))
    if tag:
        wanted = f"*_{_clean_target(tag)}.out"
        files = [f for f in files if f.name.endswith(f"_{_clean_target(tag)}.out")]
    return [str(p.relative_to(proj)) for p in files]


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _load_project_state(config, target: str):
    proj, err = _ensure_project(config, target)
    if err:
        return None, None, err
    seq = 0
    for p in (proj / "artifacts").rglob("*.out"):
        try:
            n = int(p.stem.split("_", 1)[0])
            seq = max(seq, n)
        except (ValueError, IndexError):
            continue
    return proj, seq + 1, ""


def build_vapt_tools(config) -> list[Tool]:
    """VAPT toolset. Registration is a no-op unless ``config.VAPT_ENABLED``."""

    def _active():
        if not getattr(config, "VAPT_ENABLED", False):
            return "The VAPT skill is disabled. Set VAPT_ENABLED=true in the backend .env and restart."

    def t_init(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        target = (args.get("target") or "").strip()
        proj, err = _ensure_project(config, target)
        if err:
            return err
        return (
            f"Project ready for `{target}`:\n"
            f"  root:      {proj}\n"
            f"  artifacts: {proj / 'artifacts'}\n"
            f"  scripts:   {proj / 'scripts'}\n"
            f"  reports:   {proj / 'reports'}"
        )

    def t_run(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        target = (args.get("target") or "").strip()
        command = (args.get("command") or "").strip()
        if not command:
            return "Provide a command to run."
        tag = (args.get("tag") or "run").strip() or "run"
        phase = (args.get("phase") or "misc").strip() or "misc"
        use_sudo = bool(args.get("sudo", False))
        timeout = int(args.get("timeout") or getattr(config, "VAPT_CMD_TIMEOUT", 300))

        missing = _check_available(command)
        if missing:
            return (
                f"These tools are not installed on this host and the command was NOT run: "
                f"{', '.join(missing)}.\n"
                f"Suggested install (run with sudo, then re-run):\n"
                f"  apt-get install -y {' '.join(missing)}"
            )
        proj, seq, err = _load_project_state(config, target)
        if err:
            return err

        rc, out, errout, duration = _run_shell(command, timeout, use_sudo, config, ctx.user_id)
        if rc == 98 and use_sudo:
            return f"{SUDO_MARKER}sudo credential required"

        path = _artifact_path(proj, phase, tag, seq)
        _write_artifact(
            {"target": target, "phase": phase, "tag": tag, "command": command,
             "rc": rc, "duration_s": duration, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "timeout_s": timeout, "sudo": use_sudo, "user": str(ctx.user_id)},
            out, errout, path,
        )
        cred = sudocred_get(ctx.user_id)
        out = _redact_password(out, (cred or {}).get("password", ""))
        summary = f"exit {rc} · {duration}s · artifact {path.relative_to(proj)}"
        tail = (out + "\n" + errout).strip()
        return f"`{command}`\n{summary}\n\n{(tail[:1200] or '(no output)')}"

    def t_script(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        target = (args.get("target") or "").strip()
        commands = args.get("commands") or []
        if isinstance(commands, str):
            commands = [c for c in commands.splitlines() if c.strip()]
        if not commands:
            return "Provide a list of commands to script."
        name = (args.get("name") or "batch").strip() or "batch"
        phase = (args.get("phase") or "misc").strip() or "misc"
        parallel = bool(args.get("parallel", False))
        use_sudo = bool(args.get("sudo", False))
        timeout = int(args.get("timeout") or getattr(config, "VAPT_CMD_TIMEOUT", 300))

        missing: list[str] = []
        for command in commands:
            missing.extend(_check_available(command))
        if missing:
            uniq = sorted(set(missing))
            return (
                f"These tools are not installed on this host; nothing ran:\n  {' '.join(uniq)}\n"
                f"Suggested install (sudo): apt-get install -y {' '.join(uniq)}\n"
                "After installing, ask me to re-run the batch."
            )
        proj, err = _ensure_project(config, target)
        if err:
            return err
        scripts_dir = proj / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        seq = len(list(scripts_dir.glob("*.sh"))) + 1
        script_file = scripts_dir / f"{seq:03d}_{_clean_target(phase)}_{_clean_target(name)}.sh"

        header = [
            "#!/usr/bin/env bash",
            f"# APEX VAPT batch · {phase} · {name} · {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "set -u",
            'OUT="${OUT:-.}"',
            "",
        ]
        lines: list[str] = list(header)
        if use_sudo:
            lines.append("sudo -v 2>/dev/null || true")
            lines.append("")
        for i, command in enumerate(commands, start=1):
            prefix = "sudo -n " if use_sudo else ""
            lines.append(
                f"t{i}() {{ echo \"=== [{i}] {phase}\" >> \"$OUT/{i:03d}_{_clean_target(name)}_{i}.out\"; "
                f"{prefix}{command} >> \"$OUT/{i:03d}_{_clean_target(name)}_{i}.out\" 2>&1; "
                f"echo \"rc=$?\" >> \"$OUT/{i:03d}_{_clean_target(name)}_{i}.out\"; }}"
            )
        if parallel:
            cap = max(1, int(getattr(config, "VAPT_MAX_PARALLEL", 3)))
            lines.append("n=0")
            for i in range(1, len(commands) + 1):
                lines.append(f"t{i} &")
                lines.append(f"n=$((n + 1)); if [ $n -ge {cap} ]; then wait; n=0; fi")
            lines.append("wait")
        else:
            for i in range(1, len(commands) + 1):
                lines.append(f"t{i}")
        script_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if use_sudo and not sudocred_get(ctx.user_id):
            return (
                f"`{script_file.name}` written; NOT executed — {SUDO_MARKER}sudo credential required"
            )

        src_dir = (proj / "artifacts" / "scripts").resolve()
        src_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "OUT": str(src_dir)}
        started = time.time()
        proc = subprocess.run(
            ["bash", str(script_file)], capture_output=True, text=True,
            timeout=timeout * max(1, len(commands), int(getattr(config, "VAPT_MAX_PARALLEL", 3))),
            env=env,
        )
        duration = round(time.time() - started, 1)
        summary_rows = []
        for i in range(1, len(commands) + 1):
            p = src_dir / f"{i:03d}_{_clean_target(name)}_{i}.out"
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace") or ""
                rc_line = "?"
                for line in reversed(text.splitlines()):
                    if line.startswith("rc="):
                        rc_line = line.split("=", 1)[1]
                        break
                snippet = text[:200].replace("\n", " ").strip()
                summary_rows.append(f"  {i:>2} rc={rc_line:<4} {snippet[:90]}{'…' if len(snippet) > 90 else ''}")
            else:
                summary_rows.append(f"  {i:>2} (no output written)")
        result = (
            f"Batch `{script_file.name}` · {duration}s · {'parallel' if parallel else 'sequential'}\n"
            + "\n".join(summary_rows)
            + f"\nArtifacts: {src_dir.relative_to(proj)}"
        )
        if proc.returncode != 0 or (proc.stderr or "").strip():
            err_tail = (proc.stderr or "")[-500:]
            cred_rec = sudocred_get(ctx.user_id)
            if cred_rec:
                err_tail = _redact_password(err_tail, cred_rec.get("password", ""))
            if err_tail.strip():
                result += f"\nstderr tail:\n{err_tail}"
        return result

    def t_missing(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        commands = args.get("commands") or []
        if isinstance(commands, str):
            commands = [c for c in commands.splitlines() if c.strip()]
        missing = sorted({m for c in commands for m in _check_available(c)})
        if not missing:
            return "All requested tools are installed on this host."
        return (
            "Missing tools:\n  " + " ".join(missing) + "\n"
            "Suggested install (sudo, then re-run the batch):\n"
            f"  sudo apt-get install -y {' '.join(missing)}"
        )

    def t_read(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        target = (args.get("target") or "").strip()
        artifact = (args.get("artifact") or "").strip()
        proj, err = _project_dir(config, target)
        if err:
            return err
        if not proj.exists():
            return f"No project for `{target}` yet — run vapt_project_init first."
        limit = int(args.get("limit") or 4000)
        if artifact:
            path = (proj / "artifacts" / artifact).resolve()
            if not path.is_relative_to((proj / "artifacts").resolve()):
                return "Artifact path escapes the artifacts directory."
            candidates = [path] if path.is_file() else list(proj.glob(str(proj / "artifacts") + "/**/" + artifact + "*"))
            path = candidates[0] if candidates else None
            if path is None or not path.is_file():
                return f"Artifact `{artifact}` not found."
            data = path.read_text(encoding="utf-8", errors="replace")
            return data[:limit] or "(empty artifact)"
        listing = _summarize_artifacts(proj)
        if not listing:
            return "No artifacts yet for this target."
        return "Artifacts:\n" + "\n".join("  " + x for x in listing)

    def t_projects(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        root = _projects_root(config)
        if not root.is_dir():
            return "No VAPT projects yet."
        rows = []
        for proj in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not proj.is_dir():
                continue
            artifacts = list((proj / "artifacts").rglob("*.out")) if (proj / "artifacts").is_dir() else []
            scripts = list((proj / "scripts").glob("*.sh")) if (proj / "scripts").is_dir() else []
            reports = list((proj / "reports").glob("*.md")) if (proj / "reports").is_dir() else []
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(proj.stat().st_mtime))
            rows.append(
                f"- **{proj.name}** — {len(artifacts)} artifacts, {len(scripts)} scripts, "
                f"{len(reports)} reports (touched {mtime})"
            )
        header = f"VAPT projects ({len(rows)}):"
        return header + "\n" + "\n".join(rows) if rows else "No VAPT projects yet."

    def t_destroy(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        target = (args.get("target") or "").strip()
        confirm = bool(args.get("confirm"))
        proj, err = _project_dir(config, target)
        if err:
            return err
        if not proj.is_dir():
            return f"No project for `{target}` — nothing to delete."
        items = sum(1 for _ in proj.rglob("*"))
        if not confirm:
            return (
                f"Project `{target}` has {items} items and would be deleted permanently. "
                f"Set confirm=true to proceed."
            )
        shutil.rmtree(proj, ignore_errors=True)
        return f"VAPT project `{target}` deleted ({items} items removed)."

    def t_report(args: dict, ctx: ToolContext):
        blocked = _active()
        if blocked:
            return blocked
        target = (args.get("target") or "").strip()
        body = (args.get("markdown") or args.get("body") or "").strip()
        if not body:
            return "Provide the report body as markdown."
        if not ctx.user_id:
            return "Sign in before generating a report."
        proj, err = _ensure_project(config, target)
        if err:
            return err
        reports_dir = proj / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        addendum = (args.get("addendum") or "").strip()
        if addendum:
            body += f"\n\n## Addendum — {time.strftime('%Y-%m-%d %H:%M')}\n\n{addendum}\n"
        filename = f"{_clean_target(target)}_{time.strftime('%Y-%m-%d')}.md"
        path = reports_dir / f"{uuid.uuid4().hex[:8]}_{filename}"
        path.write_text(body, encoding="utf-8")
        ttl = int(getattr(config, "VAPT_REPORT_TTL_SECONDS", 3600))
        try:
            info = path.stat()
            token = _signer(config).dumps(
                {"user": str(ctx.user_id), "path": str(path),
                 "fingerprint": [info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns],
                 "filename": filename}
            )
            url = public_url(config, f"/api/vapt/download/{token}")
        except Exception as exc:
            return f"Report written but could not create a download link: {exc}"
        return json.dumps(
            {"report_url": url, "filename": filename,
             "expires_in_seconds": ttl, "path": str(path)},
            ensure_ascii=False,
        )

    schemas = {
        "target": {"type": "string", "description": "The assessed target (hostname/URL)."},
        "command": {"type": "string", "description": "Shell command to run, e.g. 'nmap -sV target'."},
        "tag": {"type": "string", "description": "Short label for the artifact file."},
        "phase": {"type": "string", "description": "Phase, e.g. recon/web/network/auth/sca/local/report."},
        "sudo": {"type": "boolean", "description": "Wrap in sudo -S if root is needed (prompts via the popup)."},
        "timeout": {"type": "integer", "description": "Per-command timeout in seconds."},
    }

    return [
        Tool(
            "vapt_project_init",
            "Create (or reuse) the VAPT project folder for a target: artifacts/, scripts/, reports/ under the VAPT project root.",
            {"type": "object", "properties": {"target": schemas["target"]}, "required": ["target"]},
            t_init,
            dangerous=True,
        ),
        Tool(
            "vapt_run",
            "Run a single assessment command against a target, saving its output to the project artifacts, and return a summary. Checks the binary is installed first.",
            {"type": "object", "properties": dict(schemas), "required": ["target", "command"]},
            t_run,
            dangerous=True,
        ),
        Tool(
            "vapt_script",
            "Write a batch script with the given commands, run it (sequentially or in parallel), and save each command's output to artifacts. Returns a per-command result summary.",
            {"type": "object",
             "properties": {
                 **dict(schemas),
                 "name": {"type": "string", "description": "Batch name, e.g. recon-dns."},
                 "commands": {"type": "array", "items": {"type": "string"},
                              "description": "List of shell commands to run in this batch."},
                 "parallel": {"type": "boolean",
                              "description": "Run commands concurrently (capped by VAPT_MAX_PARALLEL)."},
             },
             "required": ["target", "name", "commands"]},
            t_script,
            dangerous=True,
        ),
        Tool(
            "vapt_missing",
            "Check whether a list of commands' binaries are installed; report missing tools plus the apt install command.",
            {"type": "object",
             "properties": {"commands": {"type": "array", "items": {"type": "string"}}},
             "required": ["commands"]},
            t_missing,
        ),
        Tool(
            "vapt_read",
            "Read an artifact file or list all artifacts for a target project so findings can be analyzed.",
            {"type": "object",
             "properties": {
                 "target": schemas["target"],
                 "artifact": {"type": "string", "description": "Artifact path relative to artifacts/ (blank lists all)."},
                 "limit": {"type": "integer", "description": "Max characters to return."},
             },
             "required": ["target"]},
            t_read,
        ),
        Tool(
            "vapt_projects",
            "List all VAPT projects with artifact/script/report counts so the session can be resumed.",
            {"type": "object", "properties": {}},
            t_projects,
        ),
        Tool(
            "vapt_destroy",
            "Delete a VAPT project permanently (artifacts, scripts, reports). Requires confirm=true.",
            {"type": "object",
             "properties": {
                 "target": schemas["target"],
                 "confirm": {"type": "boolean", "default": False,
                             "description": "Set true to permanently delete the project."},
             },
             "required": ["target"]},
            t_destroy,
            dangerous=True,
        ),
        Tool(
            "vapt_report",
            "Persist a professional markdown report for a target under reports/ and return an expiring download link. Call again to re-create an updated report (e.g. after new artifacts or user additions).",
            {"type": "object",
             "properties": {
                 "target": schemas["target"],
                 "markdown": {"type": "string", "description": "The full professional markdown report body."},
                 "addendum": {"type": "string",
                              "description": "Optional addition appended as an Addendum section (user-supplied notes/new findings)."},
             },
             "required": ["target", "markdown"]},
            t_report,
        ),
    ]


def _signer(config):
    key = getattr(config, "SECRET_KEY", "dev-change-me")
    return URLSafeTimedSerializer(key, salt=VAPT_SALT)


def _reports_root(config):
    return _projects_root(config)


def register_vapt_routes(app, require_user, config):
    """Sudo password endpoint + signed report downloads."""

    @app.post("/api/vapt/password")
    def vapt_password_set():
        # This in-memory, per-user session credential is used by BOTH the VAPT
        # tools and run_shell (shell skill) for elevation, so it is not gated
        # on VAPT_ENABLED — the popup must work for the shell skill alone.
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in first."}), 401
        data = request.get_json(silent=True) or {}
        password = data.get("password")
        if not isinstance(password, str) or not password:
            return jsonify({"error": "Password is required."}), 400
        save = bool(data.get("save", False))
        sudocred_set(user["id"], password, save, config)
        return jsonify({"ok": True, "saved": save, "ttl_minutes": config.VAPT_SUDO_TTL_MINUTES})

    @app.delete("/api/vapt/password")
    def vapt_password_clear():
        user = require_user()
        if user:
            sudocred_clear(user["id"])
        return jsonify({"ok": True})

    @app.get("/api/vapt/download/<token>")
    def vapt_download(token):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to download this file."}), 401
        ttl = int(getattr(config, "VAPT_REPORT_TTL_SECONDS", 3600))
        try:
            ticket = _signer(config).loads(token, max_age=ttl)
        except SignatureExpired:
            return jsonify({"error": "Download link expired. Regenerate the report."}), 410
        except BadSignature:
            return jsonify({"error": "Invalid download link."}), 404
        if not isinstance(ticket, dict) or ticket.get("user") != str(user["id"]):
            return jsonify({"error": "This link belongs to a different user."}), 403
        try:
            path = Path(ticket["path"]).resolve()
            root = _reports_root(config)
            if not path.is_relative_to(root):
                return jsonify({"error": "Invalid file location."}), 403
            if not path.is_file():
                return jsonify({"error": "File is unavailable."}), 404
            info = path.stat()
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify({"error": "File is unavailable."}), 404
        expected = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        if expected != ticket.get("fingerprint"):
            return jsonify({"error": "File changed. Generate it again."}), 409
        try:
            download_name = ticket.get("filename") or path.name
            mimetype = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
            response = send_file(
                path, as_attachment=True, download_name=download_name,
                mimetype=mimetype, conditional=False, max_age=0,
            )
        except Exception:
            return jsonify({"error": "Could not send file."}), 500
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/vapt/status")
    def vapt_status():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in first."}), 401
        cred = sudocred_get(user["id"])
        return jsonify(
            {
                "enabled": bool(getattr(config, "VAPT_ENABLED", False)),
                "projects_root": str(_projects_root(config)),
                "password": "available" if cred else "unavailable",
                "saved": bool(cred and cred.get("saved")),
            }
        )

    return app


def translate_sudo_marker(output: str) -> tuple[bool, str]:
    """Detect the APEX_SUDO:: marker in a tool result for the SSE translator."""
    if not output or SUDO_MARKER not in output:
        return False, ""
    idx = output.find(SUDO_MARKER)
    reason = output[idx + len(SUDO_MARKER):].strip() or "sudo credential required"
    return True, reason