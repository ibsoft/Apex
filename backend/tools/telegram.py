"""Telegram HTTP surface: what the settings tab needs to pair and manage the bot.

Kept separate from the poller/tools so that reading status or generating a
pairing code never depends on the agent's tool registry.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from flask import jsonify, request

from tools.telegram_tools import (
    cancel_pairing_codes,
    effective_settings,
    ensure_poller,
    get_me,
    paired_link,
    pending_pairing_code,
    poller_health,
    revoke_token,
    unlink_pair,
    validate_token,
)

#: getMe is called by the status endpoint as often as the pair modal polls it;
#: a short cache keeps the status tab from hammering api.telegram.org.
_ME_CACHE: dict = {"token": "", "at": 0.0, "result": None}
_ME_TTL = 10.0


def _bot_info(token: str) -> dict | None:
    now = time.time()
    if _ME_CACHE["token"] == token and now - _ME_CACHE["at"] < _ME_TTL:
        return _ME_CACHE["result"]
    result = get_me(token)
    _ME_CACHE.update({"token": token, "at": now, "result": result})
    return result


def _whisper_ready(config) -> bool:
    raw = str(getattr(config, "TELEGRAM_WHISPER_PYTHON", "") or "").strip()
    return bool(raw and Path(raw).exists())


def telegram_status(config, require_user) -> tuple:
    user = require_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    cfg = effective_settings(config)
    token = cfg["telegram_bot_token"]
    bot = _bot_info(token) if token else None

    from tools.sip_tools import edge_tts_command, tts_command

    engine = cfg["telegram_tts_engine"]
    voice_ok = bool(edge_tts_command() if engine == "edge" else tts_command())
    pairing = pending_pairing_code(str(user["id"])) if user.get("id") else None

    return jsonify({
        "ok": True,
        "telegram_enabled": cfg["telegram_enabled"],
        "token_set": bool(token),
        "configured": cfg["configured"],
        "bot": (
            {
                "id": bot.get("id"),
                "username": bot.get("username"),
                "first_name": bot.get("first_name"),
            } if isinstance(bot, dict) else None
        ),
        # False both when the token is missing and when getMe failed; the reason
        # is the only way the tab distinguishes them.
        "bot_error": None if isinstance(bot, dict) else "Token invalid or Telegram unreachable",
        "paired": bool(paired_link()),
        "paired_username": paired_link().get("username", ""),
        "pairing_code_active": pairing is not None,
        "pairing_code_ttl_seconds": int(getattr(config, "TELEGRAM_PAIRING_TTL_SECONDS", 600) or 600),
        "voice_replies": cfg["telegram_voice_replies"],
        "tts_engine": engine,
        "tts_available": voice_ok,
        "response_language": cfg["response_language"],
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "whisper_ready": _whisper_ready(config),
        **poller_health(),
    })


def telegram_set_token(config, require_user) -> tuple:
    user = require_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "").strip()
    if not validate_token(token):
        return jsonify({"error": "That does not look like a Telegram bot token (see BotFather)."}), 400
    bot = get_me(token)
    if not isinstance(bot, dict) or not bot.get("id"):
        return jsonify({"error": "Telegram rejected this token. Check it against BotFather."}), 400
    from db import get_db

    get_db().set_setting("telegram_bot_token", token)
    # Intentionally nothing else: the token lives only in the settings DB (never
    # an env file), so a plaintext copy is not left lying around for anyone who
    # can read the repo, and it cannot linger in a file after a forget.
    return jsonify({"ok": True, "bot_id": bot.get("id"), "bot_username": bot.get("username")})


def telegram_clear_config(config, require_user) -> tuple:
    """Forget the token and the pairing: a new bot is a new identity.

    ``revoke_token`` also tears down the import-time env fallback, so a token
    that once lived in ``backend/.env`` cannot silently come back on the next
    poll and keep the bot serving after the operator told it to stop.
    """
    user = require_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    revoke_token(config)
    return jsonify({"ok": True, "token_set": False, "paired": False})


def telegram_pair(config, require_user) -> tuple:
    user = require_user()
    if not user or not user.get("id"):
        return jsonify({"error": "unauthorized"}), 401
    cfg = effective_settings(config)
    if not cfg["telegram_bot_token"]:
        return jsonify({"error": "Store the bot token first; the pairing code is for the people on that bot."}), 400
    from tools.telegram_tools import generate_pairing_code

    code = generate_pairing_code(config, str(user["id"]))
    ensure_poller(config)
    ttl = int(getattr(config, "TELEGRAM_PAIRING_TTL_SECONDS", 600) or 600)
    bot = _bot_info(cfg["telegram_bot_token"])
    return jsonify({
        "ok": True,
        "code": code,
        "ttl_seconds": ttl,
        "expires_at": time.time() + ttl,
        "bot_username": bot.get("username") if isinstance(bot, dict) else None,
    })


def telegram_unpair(config, require_user) -> tuple:
    user = require_user()
    if not user or not user.get("id"):
        return jsonify({"error": "unauthorized"}), 401
    cancel_pairing_codes(str(user["id"]))
    unlink_pair()
    return jsonify({"ok": True, "paired": False})


def register_telegram_routes(app, require_user, config):
    """Named views, not closures over a lambda (Flask endpoint-name collision)."""

    def telegram_status_view():
        return telegram_status(config, require_user)

    def telegram_set_token_view():
        return telegram_set_token(config, require_user)

    def telegram_clear_config_view():
        return telegram_clear_config(config, require_user)

    def telegram_pair_view():
        return telegram_pair(config, require_user)

    def telegram_unpair_view():
        return telegram_unpair(config, require_user)

    app.get("/api/telegram/status")(telegram_status_view)
    app.post("/api/telegram/token")(telegram_set_token_view)
    app.post("/api/telegram/clear")(telegram_clear_config_view)
    app.post("/api/telegram/pair")(telegram_pair_view)
    app.post("/api/telegram/unpair")(telegram_unpair_view)
    ensure_poller(config)