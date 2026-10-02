"""SIP HTTP surface: what the settings tab needs before it can save an account.

Kept separate from the tool so that reading the status never depends on the
agent's tool registry, and so the settings panel has one small thing to call
instead of guessing whether the backend is reachable.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from flask import jsonify

from tools.sip_tools import (
    available_transports,
    ENV_KEYS,
    MAX_CALLS,
    tts_engine_name,
    effective_settings,
    env_file_path,
    mirror_to_env,
    missing_binaries,
    pulse_ready,
    sessions_for,
    tts_command,
)


def sip_status(config, require_user):
    """What is configured, what is installed, and whether a call could connect.

    The password is never part of this. The settings tab has to be able to show
    "server, user and password are set" without ever receiving the secret back,
    which is the same reason the value is stored under a key whose read path
    masks it.
    """
    user = require_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    cfg = effective_settings(config)
    ready, why = pulse_ready()
    if not cfg["sip_enabled"]:
        why = "SIP is turned off."
    elif not cfg["configured"]:
        why = f"Missing {', '.join(cfg['missing'])}."
    elif missing_binaries():
        why = "Missing on this host: " + ", ".join(missing_binaries())

    whisper_python = str(getattr(config, "SIP_WHISPER_PYTHON", "") or "")

    return jsonify({
        "ok": True,
        "sip_enabled": cfg["sip_enabled"],
        "configured": cfg["configured"],
        "missing_settings": cfg["missing"],
        "missing_on_host": missing_binaries(),
        "transport": cfg["sip_transport"],
        # What the tab may offer is what this host can carry, not what the code
        # allows: a TLS account on a host with no TLS module never registers.
        "allowed_transports": available_transports(),
        "server": cfg["sip_server"],
        "user": cfg["sip_user"],
        # True/false only. The secret never leaves the server in any form.
        "password_set": bool(cfg["sip_password"]),
        "domain": cfg["sip_domain"],
        "display_name": cfg["sip_display_name"],
        "outbound_proxy": cfg["sip_outbound_proxy"],
        "tts_engine": tts_engine_name(config, cfg),
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "whisper_ready": bool(whisper_python and Path(whisper_python).exists()),
        "audio_loopback_ok": ready,
        "audio_loopback_note": why,
        "env_file": str(env_file_path(config)),
        "env_keys": sorted(ENV_KEYS.values()),
        "live_calls": len(sessions_for(user["id"])),
        "max_concurrent_calls": MAX_CALLS,
        "max_duration_seconds": int(getattr(config, "SIP_MAX_DURATION_SECONDS", 600)),
    })


def sip_clear_password(config, require_user):
    """Forget the stored SIP password.

    A dedicated endpoint because the ordinary settings route deliberately
    ignores an empty ``sip_password``: the settings tab cannot display the
    stored one, so it always posts an empty box. Without a separate way to clear
    it, a wrong password could only be replaced, never removed, and the account
    would stay half-configured with no way to express that.
    """
    user = require_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    from db import get_db

    get_db().clear_settings("sip_password")
    _ok, err = mirror_to_env(config)
    payload = {"ok": True, "sip_password_set": False}
    if err:
        payload["env_file_warning"] = err
    return jsonify(payload)


def register_sip_routes(app, require_user, config):
    """Named views, not closures over a lambda.

    Flask derives an endpoint name from ``__name__``, so two ``lambda`` routes
    are both called ``<lambda>`` and the second registration overwrites the
    first with an AssertionError.
    """

    def sip_status_view():
        return sip_status(config, require_user)

    def sip_clear_password_view():
        return sip_clear_password(config, require_user)

    app.get("/api/sip/status")(sip_status_view)
    app.post("/api/sip/clear-password")(sip_clear_password_view)