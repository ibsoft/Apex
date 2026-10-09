"""APEX assistant - Flask backend.

Endpoints
---------
  /api/oauth/start        -> begin OpenAI OAuth (PKCE) login
  /api/auth/callback      -> exchange code, verify id_token, sign in
  /api/me                 -> current user + runtime settings
  /api/logout
  /api/conversations      -> list / create
  /api/conversations/<id> -> messages / delete
  /api/chat               -> streaming agent chat (SSE) - text or voice mode
  /api/skills             -> available skill packs
  /api/memory             -> vector-store CRUD + search
  /api/settings           -> runtime config (providers, engines, models)
  /api/models             -> model list for the selected provider
  /api/health
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask, Response, current_app, jsonify, redirect, request, send_file, session,
)

from agent.factory import build_engine, make_registry
from agent.base import AgentContext
from auth import (
    OAuthError,
    bearer_for_api,
    build_authorize_url,
    exchange_code,
    is_subscription_access,
    normalize_claims,
    save_tokens,
    summary,
    verify_id_token,
    verify_subscription,
)
from config import config
from security import (
    CSRF_HEADER,
    SAFE_METHODS,
    clean_name,
    clean_username,
    csrf_protect,
    csrf_token,
    new_session_id,
)
from system_auth import SystemAuth

# The only API paths a locked session may reach: what the lock screen needs to
# render itself and to let the user back in. Everything else under /api/ is
# refused with 423 while locked.
LOCK_ALLOWED_PATHS = frozenset(
    {
        "/api/auth/status",
        "/api/auth/unlock",
        "/api/auth/lock",
        "/api/auth/logout",
        "/api/auth/login",
        "/api/auth/system-users",
        "/api/health",
    }
)


def _clamp_int(value, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def _rt_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_model(
    provider_name: str,
    requested: str,
    rt: dict,
    skill_model: str,
    *,
    is_subscription: bool,
    hard_requested: bool = False,
    hard_enabled: bool = False,
    hard_model: str = "",
    allowed_models=None,
) -> tuple[str, str]:
    """Pick the model for one turn. Returns ``(model, think_hard_note)``.

    Precedence, highest first: a turn the user asked to think hard on, then the
    model the request asked for, then the saved runtime model, then the skill's
    model, then the provider default. A think-hard turn is answered once by the
    think-hard model — there is no second pass.

    ``think_hard_note`` is non-empty only when the user asked to think hard and
    that could not be honoured. It is appended to the system prompt so the model
    answers as well as it can and says why, instead of silently pretending.
    """
    model = requested or rt.get("model") or skill_model
    if not model:
        if provider_name == "codex":
            model = config.CODEX_MODEL
        elif provider_name == "ollama":
            model = rt.get("ollama_model") or config.OLLAMA_MODEL
        elif provider_name == "kimi":
            model = rt.get("kimi_model") or config.KIMI_MODEL
        elif provider_name == "torch":
            model = rt.get("torch_model") or config.TORCH_MODEL
        elif is_subscription:
            model = config.CHATGPT_MODEL
        else:
            model = config.DEFAULT_MODEL
    if not hard_requested:
        return model, ""
    hard_model = (hard_model or "").strip()
    if not hard_enabled or not hard_model:
        return model, (
            "The user asked you to think hard, but no think-hard model is configured: "
            "set THINK_HARD_MODEL and THINK_HARD_MODEL_ENABLED (or use the Think hard "
            "rows in Settings). Answer as well as you can on your current model and "
            "mention in one short line that the think-hard model is not set up yet."
        )
    # Local/self-hosted providers only accept models they actually serve, so an
    # env-only model name must not break the turn.
    if allowed_models is not None and hard_model not in allowed_models:
        return model, (
            f"The user asked you to think hard, but the configured think-hard model "
            f"({hard_model}) is not available on the {provider_name} provider. Answer as "
            "well as you can on your current model and mention in one short line that the "
            "think-hard model is unavailable for this provider."
        )
    return hard_model, ""

from db import get_db
from memory.store import get_memory
from tools.visio_tools import effective_settings as visio_settings
from models.embedders import EmbeddingManager
from models.providers import ProviderError, ProviderManager
from skills.manager import get_skill_manager, route_skill
from soul import normalize_soul, soul_prompt_block
from tools.command_router import (
    resolve_local_actions,
    router_enabled,
    router_model,
)
from tools.tasks import task_context_block
from tools.memory_tools import memory_prompt_block
from tools.sip_tools import (
    ALLOWED_TRANSPORTS,
    ALLOWED_TTS_ENGINES,
    ENV_KEYS as SIP_ENV_KEYS,
    mirror_to_env as mirror_sip_to_env,
    effective_settings as sip_settings,
    pending_reply as sip_pending_reply,
)

SIP_TRANSPORTS = ALLOWED_TRANSPORTS
SIP_TTS_ENGINES = ALLOWED_TTS_ENGINES
#: Every settings key the SIP tab owns, mirrored into the env file on save.
SIP_FIELDS = frozenset(SIP_ENV_KEYS)
#: The free-text ones. A 200-character cap matches the VISIO fields; the
#: password is included because a real account password longer than that is a
#: paste accident, and it must never reach a baresip command line unvalidated.
SIP_TEXT_FIELDS = (
    "sip_server", "sip_user", "sip_password", "sip_transport", "sip_port",
    "sip_display_name", "sip_domain", "sip_outbound_proxy", "sip_notify_to",
    "sip_tts_engine", "sip_tts_voice",
)
SIP_FIELD_MAX = 200

#: Settings keys that hold a secret. They are stripped from every HTTP response
#: by ``public_settings`` and replaced with a ``<key>_set`` boolean. SIP is the
#: only one today; the list exists so the next credential does not have to
#: rediscover this.
SECRET_SETTING_KEYS = ("sip_password",)


def _mirror_sip_to_env(config):
    """Write the effective SIP settings into the env file, logging any failure.

    Thin wrapper so the POST handler reads as one line; the reasons this cannot
    be allowed to fail a save live in ``sip_tools.mirror_to_env``.
    """
    ok, err = mirror_sip_to_env(config)
    if not ok:
        current_app.logger.warning("Could not mirror SIP settings to the env file: %s", err)


def sip_config_summary(config, rt):
    """The SIP fields /api/config hands to the settings panel.

    ``sip_password_set`` is a flag, never the secret: this payload is readable
    by anything signed in, and a settings screen does not need the value to know
    whether it has been filled in.
    """
    cfg = sip_settings(config, rt)
    return {
        "sip_enabled": cfg["sip_enabled"],
        "sip_server": cfg["sip_server"],
        "sip_user": cfg["sip_user"],
        "sip_password_set": bool(cfg["sip_password"]),
        "sip_transport": cfg["sip_transport"],
        "sip_port": cfg["sip_port"],
        "sip_display_name": cfg["sip_display_name"],
        "sip_domain": cfg["sip_domain"],
        "sip_outbound_proxy": cfg["sip_outbound_proxy"],
        "sip_notify_to": cfg["sip_notify_to"],
        "sip_tts_engine": cfg["sip_tts_engine"],
        "sip_tts_voice": cfg["sip_tts_voice"],
        "sip_configured": cfg["configured"],
    }


# One running summarizer thread per (user, conversation) to keep the
# leave-conversation endpoint single-flight and idempotent.
_summary_locks: dict[tuple[str, str], threading.Thread] = {}
_summary_locks_guard = threading.Lock()


# --- session revocation -----------------------------------------------------
# Flask's default session is a stateless signed cookie, so `session.clear()` is
# only a request for the browser to forget it. The cookie itself stays valid
# until it expires, and with SESSION_REFRESH_EACH_REQUEST on (Flask's default)
# the server re-sends it on *every* response. So a sign-out in one tab is undone
# by any request another tab had already issued: that response carries a fresh
# copy of the session and re-arms the cookie, and the next request from the tab
# that just signed out is authenticated again. The symptom is signing out
# needing two clicks, which looks like a UI bug and is really a revoked session
# that was never revoked.
#
# A revoked session id is therefore recorded here and refused in current_user().
# Entries expire with the session they invalidate, so the set stays bounded.
_REVOKED_SIDS: dict[str, float] = {}
_REVOKED_SIDS_GUARD = threading.Lock()


def revoke_session_id(sid: str, ttl_seconds: int) -> None:
    """Make a signed-out session unusable even if its cookie is replayed."""
    if not sid:
        return
    expires = time.time() + max(ttl_seconds, 60)
    with _REVOKED_SIDS_GUARD:
        _REVOKED_SIDS[sid] = expires
        now = time.time()
        for gone, when in list(_REVOKED_SIDS.items()):
            if when <= now:
                del _REVOKED_SIDS[gone]


def session_is_revoked(sid: str) -> bool:
    if not sid:
        return False
    with _REVOKED_SIDS_GUARD:
        expires = _REVOKED_SIDS.get(sid)
        if expires is None:
            return False
        if expires <= time.time():
            del _REVOKED_SIDS[sid]
            return False
        return True


def run_conversation_summary(uid: str, conv_id: str):
    """Daemon: write a conversation summary into memory; always clears its slot."""
    try:
        rt = get_db().all_settings()  # same source as create_app().runtime()
        provider_name = (rt.get("provider") or config.PROVIDER_DEFAULT).lower()
        mem = get_memory()
        if mem is None:
            return
        from memory.summarizer import summarize_conversation

        summarize_conversation(
            user_id=uid,
            conversation_id=conv_id,
            db=get_db(),
            memory=mem,
            provider_mgr=ProviderManager(
                bearer=bearer_for_api(uid),
                use_oauth_access=is_subscription_access(uid),
                runtime=rt,
            ),
            provider_name=provider_name,
            rt=rt,
            response_language=rt.get("response_language") or config.RESPONSE_LANGUAGE,
            min_messages=config.MEMORY_CONVERSATION_MIN_MESSAGES,
            window=config.MEMORY_CONVERSATION_SUMMARIZE_WINDOW,
        )
    except Exception:
        pass
    finally:
        with _summary_locks_guard:
            _summary_locks.pop((uid, conv_id), None)


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def terminal_target_note(number: int, focused_terminal: str) -> str:
    """Prompt note for a turn that named one specific terminal window.

    "run top on terminal 4" pins the request to a single session, but the words
    "open ... terminal" read like "start a new one" to a model, and it did
    exactly that: the command ran in a fresh window while the operator watched
    another one. The note settles it, and also says the number must not be
    passed because the pinned window is already the target. Empty when the turn
    was not addressed at a specific window.
    """
    if not number or number < 1 or not focused_terminal:
        return ""
    return (
        f"The operator addressed one specific terminal window for this request: terminal "
        f"#{number} (the window numbered {number} on their screen). Run the command in THAT "
        f"window. It is already the focused terminal for this turn, so do not pass the `terminal` "
        f"argument, and never open an additional terminal for this request — 'open' here means "
        f"show me it in terminal {number}, not start a new one."
    )


def _log_terminal_target(number: int, session_id: str) -> None:
    """One line per turn that named a specific terminal window.

    "run top on terminal 4" going somewhere else is only diagnosable from the
    journal: the session id logged here is the one the turn was pinned to, and
    the per-command line it can be compared against.
    """
    try:
        print(f"[terminal-target] number={number} session={session_id[:8]}", file=sys.stderr, flush=True)
    except Exception:
        pass


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
        SESSION_COOKIE_NAME=config.SESSION_COOKIE_NAME,
        # Flask's default re-sends the session cookie on every response, which
        # is how a cookie from a signed-out session gets re-armed by a response
        # another tab had in flight. The cookie is minted at sign-in instead.
        SESSION_REFRESH_EACH_REQUEST=config.SESSION_REFRESH_EACH_REQUEST,
        MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
    )

    # ---- CORS ---------------------------------------------------------------
    # An allowlist, never a reflection. Echoing back whatever Origin the caller
    # sent, together with Allow-Credentials, hands any website on the internet
    # a credentialed channel to this API. SameSite=Lax on the cookie stops the
    # session from riding along today, but that is one setting away from a full
    # account takeover, so the allowlist is the control that actually holds.
    def allowed_origins() -> set[str]:
        origins = {config.FRONTEND_URL.rstrip("/"), config.BASE_URL.rstrip("/")}
        extra = str(getattr(config, "EXTRA_ALLOWED_ORIGINS", "") or "").strip()
        for item in extra.split(","):
            if item.strip():
                origins.add(item.strip().rstrip("/"))
        return {o for o in origins if o}

    @app.after_request
    def cors(resp):
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and origin in allowed_origins():
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            # Additive, never an assignment: Flask's session handling appends to
            # Vary itself, and overwriting it here left the response marked
            # "Cookie" only, so a shared cache could hand one origin's response
            # to another.
            resp.headers.add("Vary", "Origin")
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-APEX-CSRF"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        # Stop a rendered document/JSON from being sniffed into something else.
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @app.before_request
    def handle_preflight():
        if request.method == "OPTIONS":
            return ("", 204)

    @app.before_request
    def csrf_guard():
        """Reject cross-origin state changes.

        The session cookie is the only thing that identifies the caller, and
        a browser will attach it to a form POST from anywhere. Requiring a token
        that only same-origin code can read means such a request is rejected
        before it can do anything, rather than relying on SameSite alone.
        """
        if not config.CSRF_ENABLED:
            return None
        unsafe = request.method.upper() not in SAFE_METHODS
        if not unsafe:
            return None
        expected_sid = session.get("sid", "")
        if not expected_sid:
            # No session yet: a login POST is the only legitimate case, and it
            # is exempted below because there is nothing to forge against.
            return None
        header = request.headers.get(CSRF_HEADER) or ""
        if not csrf_protect(config.SECRET_KEY, expected_sid, header, unsafe=True):
            return jsonify({"error": "invalid_csrf_token"}), 403
        return None

    @app.before_request
    def locked_session_guard():
        """A locked screen has to mean locked, not just covered by an overlay.

        The frontend hides the app behind the lock screen, but that is client
        state: a stale tab, a curl call or another window can still reach the
        API with a valid cookie. The server is the only place this can be
        enforced, so while the session is locked the API is closed to everything
        except what the lock screen itself needs.
        """
        if not config.LOCK_SCREEN_ENABLED or not session.get("locked"):
            return None
        if request.method == "OPTIONS":
            return None
        path = request.path or ""
        # Static assets and pages must stay reachable or the lock screen cannot
        # render at all; the auth endpoints are how the user gets out.
        if not path.startswith("/api/"):
            return None
        if path in LOCK_ALLOWED_PATHS:
            return None
        return jsonify({"error": "session_locked", "locked": True}), 423

    # ---- helpers ------------------------------------------------------------
    def current_user():
        # A revoked session is refused before its user is read. The cookie of a
        # signed-out session is still cryptographically valid - that is the point
        # of this check - so without it a replayed cookie walks straight back in.
        if session_is_revoked(session.get("sid", "")):
            session.clear()
            return None
        uid = session.get("user_id")
        if uid:
            return uid
        # Dev mode: no OpenAI OAuth needed. Lets the full UI (chat, memory,
        # settings) run against local providers (ollama / torch / a server key)
        # - enable explicitly with DEV_MODE=true or DEV_AUTO_LOGIN=<name>.
        if config.dev_auto_login:
            upsert_dev_session()
            return session.get("user_id")
        return None

    def upsert_dev_session():
        try:
            uid = config.DEV_AUTO_LOGIN or "apex-dev"
            name = config.DEV_AUTO_LOGIN or "Apex Dev"
            get_db().upsert_user(
                user_id=uid,
                name=name,
                email=f"{uid}@apex.local",
                picture="",
                tokens=None,
                token_scopes="",
            )
            session["user_id"] = uid
            session["name"] = name
            # A dev session is a real session: it gets a session id so the CSRF
            # guard covers it exactly as it covers a password login. A mode that
            # disabled CSRF would also disable it for OAuth and system users.
            if not session.get("sid"):
                session["sid"] = new_session_id()
                session.permanent = True
        except Exception:
            app.logger.exception("dev auto-login failed")

    def require_user():
        uid = current_user()
        if not uid:
            return None
        row = get_db().get_user(uid)
        return row

    from tools.file_search import register_file_routes
    from tools.editor_tools import register_editor_routes
    from tools.image_browser import register_image_routes
    from tools.vapt_tools import register_vapt_routes
    from tools.code_tools import register_code_routes
    from tools.shell_out import register_shell_routes
    from tools.preview_tools import register_preview_routes
    from tools.terminal_server import register_terminal_routes
    from tools.filebrowser import register_filebrowser_routes
    from tools.notepad import register_notepad_routes
    from tools.tasks import register_task_routes
    from tools.sip import register_sip_routes

    register_file_routes(app, require_user, config)
    register_editor_routes(app, require_user, config)
    register_image_routes(app, require_user, config)
    from tools.visio_tools import register_visio_routes
    register_visio_routes(app, require_user, config)
    register_vapt_routes(app, require_user, config)
    register_code_routes(app, require_user, config)
    register_shell_routes(app, require_user, config)
    register_preview_routes(app, require_user, config)
    register_terminal_routes(app, require_user, config)
    register_filebrowser_routes(app, require_user, config)
    register_notepad_routes(app, require_user, config)
    register_sip_routes(app, require_user, config)
    # Registers the Tasks tab's REST surface and starts the runner thread that
    # fires due tasks. A no-op when TASKS_ENABLED is false.
    register_task_routes(app, require_user, config)

    def runtime(dotted: bool = False):
        """Effective runtime settings: DB overrides merged over env defaults."""
        return get_db().all_settings()

    def engines_available():
        try:
            return config.agent_engines_available
        except Exception:
            return ["responses"]

    def _allowed_models(provider_name: str):
        """The models this provider can actually serve, or None for "any".

        Providers that run a local list of models (ollama, kimi) reject a name
        they do not have, so a configured router model is checked against it
        before it is used. Providers addressed over an API accept any name.
        """
        if provider_name in ("ollama", "kimi"):
            try:
                return set(model_list(provider_name))
            except Exception:
                return None
        return None

    def provider_status(user_id):
        rt = runtime()
        status = {}
        status["openai"] = {
            "available": bool(config.OPENAI_API_KEY or (config.oauth_configured and user_id and is_subscription_access(user_id)) or (config.oauth_configured and user_id and bearer_for_api(user_id))),
        }
        import shutil

        status["codex"] = {"available": bool(shutil.which(config.CODEX_BINARY)
                                               and (config.CODEX_HOME / "auth.json").is_file())}
        ollama_models = []
        try:
            from models.providers import list_ollama_models

            ollama_models = list_ollama_models()
        except Exception:
            pass
        status["ollama"] = {
            "available": bool(ollama_models),
            "models": ollama_models,
        }
        from models.providers import list_kimi_models

        status["kimi"] = {
            "available": bool(config.KIMI_API_KEY),
            "models": list_kimi_models() if config.KIMI_API_KEY else [],
        }
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            torch_avail = True
        except Exception:
            torch_avail = False
        status["torch"] = {"available": torch_avail}
        return status

    def model_list(provider: str | None = None):
        rt = runtime()
        provider = provider or rt.get("provider") or config.PROVIDER_DEFAULT
        if provider == "codex":
            models = []
            if config.CODEX_MODEL:
                models.append(config.CODEX_MODEL)
            for fallback in (config.CODE_MODEL, config.CHATGPT_MODEL, config.DEFAULT_MODEL):
                if fallback and fallback not in models:
                    models.append(fallback)
            return models
        if provider == "ollama":
            from models.providers import list_ollama_models

            return list_ollama_models() or [config.OLLAMA_MODEL, "llama3.1:8b"]
        if provider == "kimi":
            from models.providers import list_kimi_models

            return list_kimi_models()
        if provider == "torch":
            return [rt.get("torch_model") or config.TORCH_MODEL]
        return [
            config.CHATGPT_MODEL,
            "gpt-4o-mini",
            "gpt-4.1",
            "chatgpt-4o-latest",
            "gpt-4o",
        ]

    def memory_or_none():
        if config.MEMORY_ENABLED:
            return get_memory()
        return None

    # ---- auth: oauth ---------------------------------------------------------
    def oauth_signin_allowed() -> bool:
        """Whether OAuth is offered as a way to sign in at all.

        With system login on it is off by default: an OpenAI account is not a
        local account, and letting one in would hand out a session that the
        machine's own user list never approved.
        """
        if not config.oauth_configured:
            return False
        if config.SYSTEM_LOGIN_ENABLED and not config.SYSTEM_LOGIN_ALLOW_OAUTH:
            return False
        return True

    @app.get("/api/oauth/start")
    def oauth_start():
        from auth import OAuthSession

        if not oauth_signin_allowed():
            return jsonify({"error": "oauth_signin_disabled"}), 403

        payload = OAuthSession.start(session)
        url = build_authorize_url(payload["state"], payload["challenge"])
        session["apex_intent"] = request.args.get(
            "next", config.FRONTEND_URL.rstrip("/") + "/"
        )
        return redirect(url)

    @app.get("/api/auth/callback")
    def oauth_callback():
        from auth import OAuthSession

        code = request.args.get("code", "")
        state = request.args.get("state", "")
        if request.args.get("error"):
            app.logger.warning("OAuth error: %s", request.args.get("error_description"))
            return redirect(config.FRONTEND_URL)
        verifier = OAuthSession.verify_and_consume(session, state)
        if not verifier:
            return jsonify({"error": "state mismatch - re-try sign in"}), 400
        try:
            tokens = exchange_code(code, verifier)
        except OAuthError as exc:
            return jsonify({"error": str(exc)}), 502
        try:
            claims = verify_id_token(tokens.get("id_token", ""))
        except OAuthError:
            claims = {}
        profile = normalize_claims(claims)
        if not profile["sub"]:
            return jsonify({"error": "id_token missing sub claim"}), 400

        user = get_db().upsert_user(
            user_id=profile["sub"],
            name=profile["name"],
            email=profile["email"],
            picture=profile["picture"],
            tokens=tokens,
            token_scopes=config.OPENAI_OAUTH_SCOPE,
        )
        save_tokens(profile["sub"], tokens, config.OPENAI_OAUTH_SCOPE)
        session["user_id"] = profile["sub"]
        session["name"] = profile["name"] or "Apex user"
        # OAuth establishes the session just like a password login, so it needs
        # a session id. Without one the CSRF guard has nothing to check against
        # and would exempt every state-changing request for OAuth users.
        session["sid"] = new_session_id()
        session.permanent = True
        intent = session.pop("apex_intent", None) or config.FRONTEND_URL
        return redirect(intent)

    def public_settings() -> dict:
        """Runtime settings with secrets replaced by presence flags.

        Every HTTP response that echoes the settings goes through this, because
        the settings table is global: without it, `GET /api/settings` would hand
        the SIP account password to anyone signed in, and the browser would be
        holding it in memory for no reason. The unmasked values stay reachable
        only to the tool, through ``sip_settings``.

        The masked key is *removed* rather than blanked, so a client cannot
        round-trip the placeholder back and overwrite the real password.
        """
        rt = dict(runtime())
        for secret in SECRET_SETTING_KEYS:
            if secret in rt:
                rt[f"{secret}_set"] = bool(rt.get(secret))
                del rt[secret]
        return rt

    # ---- auth: session --------------------------------------------------------
    @app.get("/api/me")
    def me():
        user = require_user()
        if not user:
            return jsonify({"ok": False, "user": None}), 401
        rt = runtime()
        return jsonify(
            {
                "ok": True,
                "user": summary(user),
                "settings": public_settings(),
                "engine": rt.get("engine") or config.AGENT_ENGINE,
                "provider": rt.get("provider") or config.PROVIDER_DEFAULT,
            }
        )

    # ---- auth: system users (/etc/passwd + PAM) -------------------------------
    def _client_address() -> str:
        """Best-effort source address for the login throttle.

        X-Forwarded-For is only honoured when the request actually came through
        a configured proxy; otherwise any client could forge a header and
        escape the per-address limit.
        """
        remote = request.remote_addr or "-"
        if getattr(config, "TRUST_PROXY", False):
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()[:64]
        return remote[:64]

    def _system_auth() -> SystemAuth:
        """One instance for the life of the app, not one per request.

        The throttle lives inside SystemAuth, so building a new one per request
        would hand every caller a fresh set of attempt counters and the lockout
        after N wrong passwords would never trigger.
        """
        auth = app.extensions.get("apex_system_auth")
        if auth is None:
            auth = SystemAuth(
                service=config.SYSTEM_LOGIN_PAM_SERVICE,
                max_attempts=config.SYSTEM_LOGIN_MAX_ATTEMPTS,
                window_seconds=config.SYSTEM_LOGIN_WINDOW_SECONDS,
                lockout_seconds=config.SYSTEM_LOGIN_LOCKOUT_SECONDS,
                audit_path=config.SYSTEM_LOGIN_AUDIT_LOG,
                allowed_groups=config.SYSTEM_LOGIN_GROUPS,
            )
            app.extensions["apex_system_auth"] = auth
        return auth

    def _establish_session(user_id: str, name: str, email: str = "", picture: str = "") -> dict:
        """Create the APEX identity for a verified system account.

        The APEX user id is the system account name. Passwords never reach the
        database; only the account name, so every object APEX stores is scoped
        to a real system user and deleting the system user revokes access.
        """
        row = get_db().upsert_user(
            user_id=user_id,
            name=name,
            email=email or f"{user_id}@localhost",
            picture=picture,
            tokens=None,
            token_scopes="",
        )
        session.clear()
        session["user_id"] = user_id
        session["name"] = name
        session["sid"] = new_session_id()
        session.permanent = True
        app.permanent_session_lifetime = timedelta(hours=config.SESSION_LIFETIME_HOURS)
        return row

    @app.get("/api/auth/system-users")
    def auth_system_users():
        """Accounts offered on the login screen. Passwords are never involved."""
        if not config.SYSTEM_LOGIN_ENABLED:
            return jsonify({"ok": True, "enabled": False, "users": []})
        if not config.SYSTEM_LOGIN_SHOW_USERS:
            return jsonify({"ok": True, "enabled": True, "users": []})
        try:
            users = _system_auth().list_users()
        except Exception:
            app.logger.exception("system user lookup failed")
            users = []
        return jsonify({
            "ok": True,
            "enabled": True,
            "users": [
                {"username": u.username, "name": clean_name(u.display_name, max_length=120),
                 "initials": u.initials, "shell": u.shell}
                for u in users
            ],
        })

    @app.post("/api/auth/login")
    def auth_login():
        """Sign in with a system account."""
        if not config.SYSTEM_LOGIN_ENABLED:
            return jsonify({"error": "system_login_disabled"}), 404
        payload = request.get_json(silent=True) or {}
        username = clean_username(payload.get("username"))
        password = payload.get("password")
        # Not cleaned: sanitizing a password would silently change a valid one.
        # It is a str, capped, and handed straight to PAM.
        password = password if isinstance(password, str) else ""
        if not password or len(password) > 1024:
            return jsonify({"error": "Enter your username and password."}), 400

        auth = _system_auth()
        result = auth.authenticate(username, password, _client_address())
        if not result.ok or not result.user:
            body = {"error": result.error or "Incorrect username or password."}
            if result.locked:
                body["retry_after"] = auth.retry_after(username, _client_address())
            return jsonify(body), 401

        user = result.user
        row = _establish_session(user.username, user.display_name)
        return jsonify({
            "ok": True,
            "user": summary(row),
            "csrf_token": csrf_token(config.SECRET_KEY, session["sid"]),
        })

    @app.post("/api/auth/unlock")
    def auth_unlock():
        """Leave the lock screen.

        Re-checks the password rather than trusting a flag in the client, so a
        locked machine is genuinely locked even if the tab was left open.
        """
        if not config.LOCK_SCREEN_ENABLED:
            return jsonify({"error": "lock_disabled"}), 404
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "unauthorized"}), 401
        payload = request.get_json(silent=True) or {}
        password = payload.get("password")
        password = password if isinstance(password, str) else ""
        if not password or len(password) > 1024:
            return jsonify({"error": "Enter your password."}), 400

        auth = _system_auth()
        address = _client_address()
        result = auth.authenticate(user_id, password, address)
        if not result.ok:
            body = {"error": result.error or "Incorrect password."}
            if result.locked:
                body["retry_after"] = auth.retry_after(user_id, address)
            return jsonify(body), 401

        # Rotate the session id (new CSRF token, same signed-in user) and clear
        # the lock flag server-side. Clearing it only in the browser would leave
        # the API refusing every request with 423 for the rest of the session.
        session["sid"] = new_session_id()
        session.pop("locked", None)
        session["unlocked_at"] = int(time.time())
        return jsonify({
            "ok": True,
            "csrf_token": csrf_token(config.SECRET_KEY, session["sid"]),
        })

    @app.post("/api/auth/lock")
    def auth_lock():
        if not session.get("user_id"):
            return jsonify({"error": "unauthorized"}), 401
        session["locked"] = True
        # A new session id invalidates any CSRF token the locked client cached,
        # so the replacement token has to travel back in this response: the
        # client cannot compute it, and the very next request it makes is the
        # unlock POST.
        session["sid"] = new_session_id()
        return jsonify({
            "ok": True,
            "csrf_token": csrf_token(config.SECRET_KEY, session["sid"]),
        })

    @app.post("/api/auth/logout")
    def auth_logout():
        # Revoking the session id is what actually ends it: session.clear() only
        # asks the browser to drop a cookie that stays valid until it expires,
        # and SESSION_REFRESH_EACH_REQUEST is off precisely so no response
        # re-arms it. Without the revoke, signing out in one tab is undone by
        # whatever another tab had in flight, and it takes two attempts.
        revoke_session_id(
            session.get("sid", ""), config.SESSION_LIFETIME_HOURS * 3600
        )
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/auth/status")
    def auth_status():
        """What the client needs to render the right screen.

        Carries the CSRF token, so a page load is enough to obtain one and no
        state-changing request has to run without protection.
        """
        sid = session.get("sid", "")
        return jsonify({
            "ok": True,
            "system_login_enabled": bool(config.SYSTEM_LOGIN_ENABLED),
            "lock_enabled": bool(config.LOCK_SCREEN_ENABLED),
            "authenticated": bool(session.get("user_id")),
            "locked": bool(session.get("locked")),
            "oauth_available": oauth_signin_allowed(),
            "csrf_token": csrf_token(config.SECRET_KEY, sid) if sid else "",
        })

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "oauth_configured": config.oauth_configured,
                "engines": engines_available(),
            }
        )

    # ---- self health / healing (read-only diagnostics by default) -----------
    _self_health_cache: dict = {"at": 0, "result": None}
    _self_health_lock = threading.Lock()

    @app.get("/api/self/health")
    def self_health():
        """Return cached self-health diagnostics. Use ?run=1 to refresh.

        This endpoint never modifies code; it only reports test/build status so
        the autonomous engine (or user) can decide whether to ask for a fix.
        """
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        run = request.args.get("run", "0") in {"1", "true", "yes"}
        with _self_health_lock:
            stale = run or (time.time() - _self_health_cache["at"] > 300)
            if stale or _self_health_cache["result"] is None:
                result = _run_self_diagnostics()
                _self_health_cache.update(at=time.time(), result=result)
            return jsonify({"ok": True, "health": _self_health_cache["result"]})

    def _run_self_diagnostics() -> dict:
        root = Path(__file__).resolve().parent.parent
        out: dict = {"checks": [], "overall": "unknown"}

        def run(cmd: list[str], cwd: Path, label: str, timeout: int = 120, env: dict | None = None):
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
                ok = proc.returncode == 0
                return {
                    "label": label,
                    "ok": ok,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout[-800:] if proc.stdout else "",
                    "stderr": proc.stderr[-800:] if proc.stderr else "",
                }
            except subprocess.TimeoutExpired as exc:
                def decoded(value):
                    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")

                return {
                    "label": label,
                    "ok": False,
                    "error": f"timed out after {timeout}s",
                    "stdout": decoded(exc.stdout)[-400:],
                    "stderr": decoded(exc.stderr)[-400:],
                }
            except Exception as exc:
                return {"label": label, "ok": False, "error": str(exc)}

        venv_python = root / ".venv" / "bin" / "python"
        pytest_cmd = [str(venv_python), "-m", "pytest", "backend/tests", "-q"] if venv_python.exists() else ["python", "-m", "pytest", "backend/tests", "-q"]
        try:
            with tempfile.TemporaryDirectory(prefix="apex-self-check-data-") as directory:
                test_env = {
                    **os.environ,
                    "DATA_DIR": directory,
                    "DB_PATH": str(Path(directory) / "apex.db"),
                    "DEV_MODE": "true",
                    "DEV_AUTO_LOGIN": "apex-self-check",
                }
                out["checks"].append(run(pytest_cmd, root, "backend tests", env=test_env))
        except Exception as exc:
            out["checks"].append({"label": "backend tests", "ok": False, "error": str(exc)})
        # Next builds can rewrite both output and TypeScript configuration.
        # Diagnose a snapshot so autonomous checks cannot disrupt the live UI.
        try:
            with tempfile.TemporaryDirectory(prefix="apex-self-check-") as directory:
                frontend = root / "frontend"
                snapshot = Path(directory) / "frontend"
                shutil.copytree(
                    frontend, snapshot,
                    ignore=shutil.ignore_patterns("node_modules", ".next*", "*.tsbuildinfo", "certificates"),
                )
                if (frontend / "node_modules").exists():
                    (snapshot / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
                build_env = {**os.environ, "NEXT_DIST_DIR": ".next", "NEXT_TELEMETRY_DISABLED": "1"}
                out["checks"].append(run(["npm", "run", "build"], snapshot, "frontend build", timeout=300, env=build_env))
        except Exception as exc:
            out["checks"].append({"label": "frontend build", "ok": False, "error": str(exc)})
        out["overall"] = "healthy" if all(c.get("ok") for c in out["checks"]) else "needs_attention"
        return out

    @app.get("/api/logout")
    def logout():
        from tools.vapt_tools import sudocred_clear
        uid = current_user()
        if uid:
            sudocred_clear(str(uid))
        session.clear()
        return redirect(request.referrer or config.FRONTEND_URL)

    # ---- config / models / skills ---------------------------------------------
    @app.get("/api/config")
    def app_config():
        uid = current_user()
        rt = runtime()
        mem = memory_or_none()
        return jsonify(
            {
                "ok": True,
                "oauth_configured": config.oauth_configured,
                "logged_in": bool(uid),
                "engine": rt.get("engine") or config.AGENT_ENGINE,
                "provider": rt.get("provider") or config.PROVIDER_DEFAULT,
                "providers": provider_status(uid),
                "engines": engines_available(),
                "models": model_list(),
                **visio_settings(config, rt),
                **sip_config_summary(config, rt),
                "think_hard_model": str(rt.get("think_hard_model") or config.THINK_HARD_MODEL or "").strip(),
                "think_hard_model_enabled": _rt_bool(rt.get("think_hard_model_enabled")) if "think_hard_model_enabled" in rt else config.THINK_HARD_MODEL_ENABLED,
                "memory_enabled": bool(mem),
                "embedding": (get_memory().embedding_name if mem else None),
                "wake_word": rt.get("wake_word") or config.WAKE_WORD,
                "follow_up_seconds": int(rt.get("follow_up_seconds") or config.FOLLOW_UP_SECONDS),
                "voice": rt.get("voice") or config.VOICE,
                "response_language": rt.get("response_language") or config.RESPONSE_LANGUAGE,
                "soul": normalize_soul(rt.get("soul")),
                "soul_max_chars": config.SOUL_MAX_CHARS,
                "autonomous_mode": _rt_bool(rt.get("autonomous_mode")) if "autonomous_mode" in rt else config.AUTONOMOUS_MODE,
                "humor_level": _clamp_int(rt.get("humor_level"), 1, 100) if "humor_level" in rt else config.HUMOR_LEVEL,
                "sarcasm_level": _clamp_int(rt.get("sarcasm_level"), 1, 100) if "sarcasm_level" in rt else config.SARCASM_LEVEL,
                "autonomous_voice_budget": _clamp_int(rt.get("autonomous_voice_budget"), 0, 100) if "autonomous_voice_budget" in rt else config.AUTONOMOUS_VOICE_BUDGET,
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "builtin": s.builtin,
                        "tools": s.tools,
                        "model": s.model,
                    }
                    for s in get_skill_manager().all()
                ],
            }
        )

    @app.get("/api/models")
    def models():
        return jsonify({"models": model_list(request.args.get("provider"))})

    @app.get("/api/skills")
    def skills():
        return jsonify(
            [
                {
                    "name": s.name,
                    "description": s.description,
                    "builtin": s.builtin,
                    "tools": s.tools,
                    "model": s.model,
                }
                for s in get_skill_manager().all()
            ]
        )

    @app.delete("/api/skills/<name>")
    def delete_skill(name: str):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        skill = get_skill_manager().get(name)
        if skill is None:
            return jsonify({"error": "skill not found"}), 404
        if skill.builtin:
            return jsonify({"error": "cannot delete built-in skill"}), 403
        if get_skill_manager().delete(name):
            return jsonify({"ok": True})
        return jsonify({"error": "could not delete skill"}), 500

    @app.get("/api/visio/cameras")
    def visio_cameras():
        if not require_user():
            return jsonify({"error": "unauthorized"}), 401
        from tools.visio_tools import camera_devices
        import shutil
        return jsonify({"cameras": camera_devices(), "ffmpeg_available": bool(shutil.which("ffmpeg"))})

    # ---- settings --------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(
            {
                "settings": public_settings(),
                "providers": provider_status(user["id"]),
                "engines": engines_available(),
                "models": model_list(),
            }
        )

    @app.post("/api/settings")
    def set_settings():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        allowed = {
            "engine", "provider", "model", "temperature", "voice", "response_language",
            "wake_word", "follow_up_seconds", "tts_enabled", "memory_enabled",
            "embedding_backend", "strict_tool_json", "ollama_base_url",
            "base_url", "torch_model", "model_extra", "autonomous_mode",
            "humor_level", "sarcasm_level", "autonomous_voice_budget", "soul",
            "think_hard_model", "think_hard_model_enabled",
            "visio_enabled", "visio_provider", "visio_model", "visio_camera",
            "sip_enabled", "sip_server", "sip_user", "sip_password", "sip_transport",
            "sip_port", "sip_display_name", "sip_domain", "sip_outbound_proxy",
            "sip_notify_to", "sip_tts_engine", "sip_tts_voice",
        }
        if "visio_provider" in data and data["visio_provider"] not in ("openai", "ollama"):
            return jsonify({"error": "VISIO provider must be openai or ollama"}), 400
        for key in ("visio_model", "visio_camera"):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 200):
                return jsonify({"error": f"Invalid {key}"}), 400
        if data.get("visio_camera") and not re.fullmatch(r"/dev/video[0-9]+", data["visio_camera"]):
            return jsonify({"error": "VISIO camera must be /dev/videoN or empty"}), 400
        for key in SIP_TEXT_FIELDS:
            if key in data and (not isinstance(data[key], str) or len(data[key]) > SIP_FIELD_MAX):
                return jsonify({"error": f"Invalid {key}"}), 400
        # A boolean posted as a list or dict would stringify to something
        # confidently falsy and quietly switch SIP off.
        if "sip_enabled" in data and not isinstance(data["sip_enabled"], (bool, int, str)):
            return jsonify({"error": "Invalid sip_enabled"}), 400
        # Rejected on save, not normalised on read: a silently rewritten
        # transport would register on udp while the operator believes they
        # configured tls.
        if "sip_transport" in data and str(data["sip_transport"]).lower() not in SIP_TRANSPORTS:
            return jsonify({"error": f"SIP transport must be one of {', '.join(SIP_TRANSPORTS)}"}), 400
        if "sip_tts_engine" in data and str(data["sip_tts_engine"]).lower() not in SIP_TTS_ENGINES:
            return jsonify({"error": f"SIP TTS engine must be one of {', '.join(SIP_TTS_ENGINES)}"}), 400
        if data.get("sip_port"):
            try:
                port = int(data["sip_port"])
            except (TypeError, ValueError):
                return jsonify({"error": "SIP port must be a number or empty"}), 400
            if not 1 <= port <= 65535:
                return jsonify({"error": "SIP port must be between 1 and 65535"}), 400
        # A stored password is never sent back to the browser, so the settings tab
        # renders an empty box for it. Posting that empty box back would
        # otherwise silently destroy a working account on any unrelated save of
        # another SIP field, so an empty value keeps what is stored and clearing
        # the password is a separate explicit action.
        if "sip_password" in data and not str(data["sip_password"] or "").strip():
            data.pop("sip_password")
        sip_patch = {k for k in data if k in SIP_FIELDS}
        for key, value in data.items():
            if key not in allowed:
                continue
            if key == "temperature":
                value = float(value)
            if key in ("follow_up_seconds",):
                value = int(value)
            if key == "response_language":
                value = str(value).lower()
                if value not in {"en", "el"}:
                    continue
            if key == "autonomous_mode":
                value = str(value).strip().lower() in {"1", "true", "yes", "on"}
            if key in ("humor_level", "sarcasm_level"):
                value = _clamp_int(value, 1, 100)
            if key == "autonomous_voice_budget":
                value = _clamp_int(value, 0, 100)
            if key == "soul":
                value = normalize_soul(value)
            if key == "think_hard_model":
                value = str(value or "").strip()
            if key in {"think_hard_model_enabled", "visio_enabled"}:
                value = str(value).strip().lower() in {"1", "true", "yes", "on"}
            if key in SIP_FIELDS:
                if key == "sip_enabled":
                    value = str(value).strip().lower() in {"1", "true", "yes", "on"}
                elif key == "sip_transport":
                    value = str(value).strip().lower()
                elif key == "sip_tts_engine":
                    value = str(value).strip().lower()
                else:
                    value = str(value or "").strip()
            get_db().set_setting(key, value)
        # The settings tab is the source of truth, but the standalone helper and
        # a shell-started backend both read the env file, so every SIP_* value is
        # mirrored there. Done after the DB writes and only when this request
        # actually touched a SIP field, because the env file holds the OpenAI
        # keys too and rewriting it on every keystroke would be churn.
        if sip_patch:
            _mirror_sip_to_env(config)
        get_skill_manager().refresh()
        return jsonify({"ok": True, "settings": public_settings()})

    # ---- conversations ------------------------------------------------------------
    @app.get("/api/conversations")
    def list_convos():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        convos = get_db().list_conversations(user["id"])
        out = []
        for c in convos:
            count = get_db().list_messages(c["id"])
            out.append({
                "id": c["id"], "title": c["title"], "skill": c["skill"],
                "engine": c["engine"], "created_at": c["created_at"],
                "updated_at": c["updated_at"], "messages": len(count),
            })
        return jsonify(out)

    @app.post("/api/conversations")
    def create_convo():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        conv = get_db().create_conversation(
            user["id"], title=data.get("title", ""),
            skill=data.get("skill", ""), engine=data.get("engine", ""),
        )
        return jsonify(conv)

    @app.get("/api/conversations/<conv_id>/messages")
    def get_messages(conv_id: str):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        conv = get_db().get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            return jsonify({"error": "not found"}), 404
        msgs = [
            {"role": m["role"], "content": m["content"], "meta": m["meta"]}
            for m in get_db().list_messages(conv_id)
        ]
        return jsonify({"conversation": conv, "messages": msgs})

    @app.post("/api/conversations/<conv_id>/summarize")
    def summarize_convo(conv_id: str):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        conv = get_db().get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            return jsonify({"error": "not found"}), 404
        mem = get_memory()
        if mem is None or not config.MEMORY_CONVERSATION_SUMMARIZE:
            return jsonify({"error": "unavailable"}), 400
        key = (user["id"], conv_id)
        with _summary_locks_guard:
            running = _summary_locks.get(key)
            if running is not None and running.is_alive():
                return jsonify({"ok": True, "status": "already_running"}), 202
            worker = threading.Thread(
                target=run_conversation_summary,
                args=(user["id"], conv_id),
                daemon=True,
            )
            _summary_locks[key] = worker
            worker.start()
        return jsonify({"ok": True, "status": "started"}), 202

    @app.delete("/api/conversations/<conv_id>")
    def delete_convo(conv_id: str):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        conv = get_db().get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            return jsonify({"error": "not found"}), 404
        get_db().delete_conversation(conv_id)
        mem = get_memory()
        if mem is not None:
            try:
                mem.forget_conversation(user["id"], conv_id)
            except Exception:
                pass
        return jsonify({"ok": True})

    # ---- memory ----------------------------------------------------------------
    @app.get("/api/memory")
    def memory_list():
        user = require_user()
        mem = memory_or_none()
        if not user or mem is None:
            return jsonify({"error": "unavailable"}), (401 if not user else 400)
        return jsonify({"count": mem.count(user["id"]), "entries": mem.list_all(user["id"])})

    @app.post("/api/memory")
    def memory_add():
        user = require_user()
        mem = memory_or_none()
        if not user or mem is None:
            return jsonify({"error": "unavailable"}), (401 if not user else 400)
        data = request.get_json(silent=True) or {}
        mem_id = mem.remember(
            user["id"], data.get("text", ""),
            category=data.get("category", "general"),
        )
        return jsonify({"ok": True, "id": mem_id})

    @app.post("/api/memory/search")
    def memory_search():
        user = require_user()
        mem = memory_or_none()
        if not user or mem is None:
            return jsonify({"error": "unavailable"}), (401 if not user else 400)
        data = request.get_json(silent=True) or {}
        hits = mem.recall(user["id"], data.get("query", ""), int(data.get("n", 5)))
        return jsonify(hits)

    @app.delete("/api/memory")
    def memory_delete():
        user = require_user()
        mem = memory_or_none()
        if not user or mem is None:
            return jsonify({"error": "unavailable"}), (401 if not user else 400)
        data = request.get_json(silent=True) or {}
        if data.get("all"):
            deleted = mem.clear(user["id"])
        else:
            deleted = mem.forget(user["id"], data.get("ids", []))
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/memory/upload")
    def memory_upload():
        user = require_user()
        mem = memory_or_none()
        if not user or mem is None:
            return jsonify({"error": "unavailable"}), (401 if not user else 400)
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "no files provided"}), 400

        from memory.documents import extract_text, chunk_text

        max_size = 10 * 1024 * 1024
        chunk_size = int(request.form.get("chunk_size") or 800)
        overlap = int(request.form.get("overlap") or 100)
        results = []
        total = 0

        for f in files:
            if not f.filename:
                continue
            data = f.read()
            if len(data) > max_size:
                results.append({"filename": f.filename, "error": "file too large (>10MB)"})
                continue
            try:
                text = extract_text(f.filename, data)
                chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
                if not chunks:
                    results.append({"filename": f.filename, "chunks": 0})
                    continue
                source = Path(f.filename).name
                items = [
                    (chunk, {"category": "document", "source": source, "chunk_index": i})
                    for i, chunk in enumerate(chunks)
                ]
                ids = mem.remember_chunks(user["id"], items)
                total += len(ids)
                results.append({"filename": f.filename, "chunks": len(ids)})
            except Exception as exc:
                results.append({"filename": f.filename, "error": str(exc)})

        return jsonify({"ok": True, "total": total, "files": results})

    # ---- local command reasoning ---------------------------------------------
    @app.post("/api/resolve-command")
    def resolve_command():
        """Ask a model which window actions an unrecognized utterance asks for.

        The browser's own parsers run first and still win; this is only reached
        when one of them missed, which used to mean the utterance went to the
        agent. The agent cannot un-minimize a window, so a miss was a turn that
        produced a reply and no change.

        The browser sends the catalogue of actions it can perform and the state
        of the screen, and validates the answer itself against the same
        catalogue - so this route never decides what is allowed, only what was
        meant. Every failure answers 200 with an empty list, because the caller's
        next step for "no idea" and for "provider is down" is the same one.
        """
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if not router_enabled():
            return jsonify({"actions": []})
        data = request.get_json(silent=True) or {}
        utterance = str(data.get("text") or "").strip()
        catalogue = str(data.get("catalogue") or "").strip()
        state = str(data.get("state") or "").strip()
        if not utterance or not catalogue:
            return jsonify({"actions": []})

        rt = runtime()
        uid = user["id"]
        provider_name = (rt.get("provider") or config.PROVIDER_DEFAULT).lower()
        # A per-user setting wins over the config default, the same way the
        # think-hard model does, so the router can be retuned without a restart.
        configured_model = str(rt.get("command_router_model") or config.COMMAND_ROUTER_MODEL)
        # Asking a local provider what models it has is a network call, so it is
        # only made when a dedicated model was actually configured - otherwise
        # the provider's default is used and the question does not arise.
        model = ""
        if configured_model:
            model = router_model(
                provider_name,
                allowed_models=_allowed_models(provider_name),
                configured=configured_model,
            )
        try:
            provider = ProviderManager(
                bearer=bearer_for_api(uid),
                use_oauth_access=is_subscription_access(uid),
                runtime=rt,
            ).build(provider_name, model or None)
        except ProviderError:
            # An unbuildable provider is the ordinary "not configured" case for a
            # user who has never set an API key. It is not worth a traceback, and
            # it must read as the same thing as any other fallback.
            return jsonify({"actions": []})
        except Exception:
            app.logger.exception("command router provider unavailable")
            return jsonify({"actions": []})

        actions = resolve_local_actions(
            utterance=utterance,
            language=str(data.get("language") or config.RESPONSE_LANGUAGE),
            catalogue=catalogue,
            state=state,
            provider=provider,
        )
        return jsonify({"actions": actions})

    # ---- chat ----------------------------------------------------------------
    @app.post("/api/chat")
    def chat():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        user_text = (data.get("message") or "").strip()
        if not user_text:
            return jsonify({"error": "empty message"}), 400

        rt = runtime()
        uid = user["id"]
        db = get_db()
        store_messages = bool(data.get("store_messages", True))

        # resolve conversation
        conv_id = data.get("conversation_id") or ""
        conv = db.get_conversation(conv_id) if conv_id else None
        if conv and conv["user_id"] != uid:
            return jsonify({"error": "not allowed"}), 403
        if store_messages:
            if conv is None:
                title = user_text[:48] + ("…" if len(user_text) > 48 else "")
                conv = db.create_conversation(uid, title=title)
            elif not db.list_messages(conv["id"]) and (conv.get("title") or "") in ("", "New conversation"):
                # The UI pre-creates threads via POST /api/conversations with no
                # title, so a fresh install would show "New conversation" for
                # every thread. Name an empty thread after its first post.
                title = user_text[:48] + ("…" if len(user_text) > 48 else "")
                db.update_conversation(conv["id"], title=title)
                conv["title"] = title
        else:
            # Ephemeral turns (e.g. autonomous nudges) use the requested
            # conversation for context but never create or modify history.
            if conv is None:
                conv = {"id": str(uuid.uuid4()), "user_id": uid, "skill": ""}

        skill_name = data.get("skill") or conv["skill"] or "general"
        if store_messages:
            db.update_conversation(conv["id"], skill=skill_name)

        engine_name = (rt.get("engine") or config.AGENT_ENGINE).lower()
        provider_name = (rt.get("provider") or config.PROVIDER_DEFAULT).lower()
        if provider_name == "codex":
            engine_name = "responses"
        voice_mode = bool(data.get("voice_mode", False))

        # persist user message
        if store_messages:
            db.add_message(conv["id"], "user", user_text, {"voice": voice_mode})

        bearer = bearer_for_api(uid)
        mem = memory_or_none()
        provider_mgr = ProviderManager(
            bearer=bearer,
            use_oauth_access=is_subscription_access(uid),
            runtime=rt,
        )

        # skill model override, but only when compatible with the provider
        skill_obj = get_skill_manager().select(skill_name)
        skill_model = skill_obj.model
        if skill_model:
            allowed_models = set(model_list(provider_name))
            if provider_name == "ollama" and skill_model not in allowed_models:
                skill_model = ""
            elif provider_name == "kimi" and skill_model not in allowed_models:
                skill_model = ""
            # openai / codex / torch accept the skill model as-is

        # resolve model per provider, honouring a per-turn "think hard" request
        model, think_hard_note = resolve_model(
            provider_name,
            str(data.get("model") or ""),
            rt,
            skill_model,
            is_subscription=is_subscription_access(uid),
            hard_requested=_rt_bool(data.get("think_hard")),
            hard_enabled=(
                _rt_bool(rt.get("think_hard_model_enabled"))
                if "think_hard_model_enabled" in rt
                else config.THINK_HARD_MODEL_ENABLED
            ),
            hard_model=str(rt.get("think_hard_model") or config.THINK_HARD_MODEL or ""),
            allowed_models=(
                set(model_list(provider_name)) if provider_name in ("ollama", "kimi") else None
            ),
        )

        try:
            provider = provider_mgr.build(provider_name, model)
        except ProviderError as exc:
            return jsonify({"error": str(exc)}), 502

        # A pending call plan answers the operator's reply to the plan. It is
        # checked before the skill router because the reply ("yes", "go ahead")
        # names no skill at all: routed normally it would land in general, which
        # has no sip_call, and the model would report that it cannot place calls
        # while holding the plan to do exactly that.
        routed_skill = None
        sip_reply = sip_pending_reply(user_text, uid, conv["id"])
        if sip_reply == "confirm":
            routed_skill = "SIP"
            skill_name = "SIP"
            skill_obj = get_skill_manager().select("SIP")
        elif sip_reply == "decline":
            skill_name = "general"
            skill_obj = get_skill_manager().select("general")

        # Auto-route from the general skill to the best specialist skill.
        # The conversation stays in general mode; routing is per-turn.
        if routed_skill is None and sip_reply is None and skill_name == "general" \
                and config.AUTO_ROUTE_FROM_GENERAL:
            all_skills = get_skill_manager().all()
            routed = route_skill(user_text, all_skills, provider, fallback="general")
            if routed != "general":
                routed_skill = routed
                skill_name = routed
                skill_obj = get_skill_manager().select(skill_name)

        # history for agent (exclude the just-added user message until ready)
        history = []
        for m in db.list_messages(conv["id"]):
            if m["role"] == "user" and m["content"] == user_text:
                history.append({"role": "user", "content": user_text})
                continue
            if m["role"] == "assistant":
                history.append({"role": "assistant", "content": m["content"]})
            elif m["role"] == "user":
                history.append({"role": "user", "content": m["content"]})
            if len(history) > 60:
                history = history[-60:]

        memory_block = ""
        if mem:
            memory_block = memory_prompt_block(uid, mem, user_text, config.MEMORY_RECALL_DEFAULT)

        system_prompt = get_skill_manager().build_system_prompt(
            skill_name,
            memory_block=memory_block,
            voice_mode=voice_mode,
            user_name=session.get("name") or user.get("name") or "",
            response_language=rt.get("response_language") or config.RESPONSE_LANGUAGE,
            # Operator persona from the settings tab. Last so it stays the most
            # recent instruction; empty by default and never persisted to history.
            extra=soul_prompt_block(rt.get("soul")),
        )

        # Frontend sends a compact, non-persisted inventory of open desktop
        # windows so the model can act on "the second window". It is only ever
        # added to the model-facing prompt, never to stored history.
        window_context = str(data.get("window_context") or "").strip()
        if window_context:
            system_prompt = system_prompt.rstrip() + "\n\n" + window_context

        # What the model has scheduled and how the last runs went. The runs
        # happen on a background thread with no browser attached, so nothing in
        # this conversation's history mentions them - without this block the
        # model cannot answer "did the disk check run?" or notice a task that
        # has been failing every night.
        task_context = task_context_block(uid, db)
        if task_context:
            system_prompt = system_prompt.rstrip() + "\n\n" + task_context

        if think_hard_note:
            system_prompt += "\n\n" + think_hard_note

        # The frontend knows which terminal window is focused; let terminal tools
        # default to it so "run/write on the focused terminal" is deterministic.
        focused_terminal = str(data.get("focused_terminal") or "").strip().lower()
        # Terminal session ids in the order the operator sees them; a number they
        # speak is resolved against this, not the backend's own session order.
        raw_map = data.get("terminal_map") or []
        terminal_map = [str(x).strip().lower() for x in raw_map if str(x).strip()] if isinstance(raw_map, list) else []
        # The operator named a specific window ("run top on terminal 4"). That
        # request is already pinned to one session, so tell the model plainly:
        # words like "open" must not be read as "use a fresh terminal", and it
        # must not pass a number either — the pinned window IS the target.
        try:
            terminal_target = int(data.get("terminal_target") or 0)
        except (TypeError, ValueError):
            terminal_target = 0
        target_note = terminal_target_note(terminal_target, focused_terminal)
        if target_note:
            system_prompt = system_prompt.rstrip() + "\n\n" + target_note
            _log_terminal_target(terminal_target, focused_terminal)

        output_destination = "notepad" if data.get("output_destination") == "notepad" else ""
        if output_destination:
            system_prompt += "\n\nThe user explicitly requested output in the live Notepad app. Do not generate a file or preview window as a substitute. For command manuals use noninteractive plain-text output (for example MANPAGER=cat man df, removing overstrike formatting if needed). run_shell automatically delivers its captured output to Notepad for this request; do not duplicate it with notepad_control.write. For other tools or generated prose, call notepad_control with write or replace and the actual content. Opening Notepad alone does not fulfill the request. Report failures honestly."
        tools = make_registry(memory=mem)
        ctx = AgentContext(
            user_id=uid,
            conversation_id=conv["id"],
            system_prompt=system_prompt,
            history=history,
            provider=provider,
            provider_kind=provider_name,
            engine_name=engine_name,
            tools=tools,
            skill_tools=list(skill_obj.tools),
            # getattr: a stubbed/legacy skill object may predate these fields.
            require_tool=bool(getattr(skill_obj, "require_tool", False)),
            exclude_tools=list(getattr(skill_obj, "exclude_tools", ()) or ()),
            memory=mem,
            runtime=rt,
            voice_mode=voice_mode,
            user_name=session.get("name") or user.get("name") or "",
            focused_terminal=focused_terminal,
            terminal_map=terminal_map,
            output_destination=output_destination,
        )
        engine = build_engine(engine_name, ctx)

        def meta_event():
            return {
                "type": "meta",
                "conversation_id": conv["id"],
                "engine": engine_name,
                "provider": provider_name,
                "model": model,
                "skill": skill_name,
                "voice": voice_mode,
            }

        def stream_gen():
            yield event_ss(meta_event())
            assistant_parts: list[str] = []
            tool_events = []
            error_seen = False
            usage = {}
            try:
                for ev in engine.stream():
                    if ev["type"] == "text_delta":
                        assistant_parts.append(ev["content"])
                    elif ev["type"] == "tool_result":
                        # Record EVERY tool, not just file_search. Restricting
                        # this to one name made `tools: []` in the stored message
                        # look like "the model ran nothing" when it had in fact
                        # run terminal_command - which sent the investigation
                        # after a phantom bug for a whole round.
                        tool_events.append({
                            "name": ev.get("name"),
                            "output": ev.get("output", ""),
                            "running": False,
                        })
                        # Per-tool side effects. These used to be `elif` branches
                        # AFTER this one, so the branch above swallowed every
                        # tool_result and none of them ever ran: the panel never
                        # refreshed after create_skill, and the VAPT sudo and
                        # CODE token popups never appeared. They share this
                        # branch now; `ev` itself is yielded once, below.
                        if ev.get("name") == "create_skill":
                            # Notify the UI that the skill list has changed so
                            # the new skill appears without a manual refresh.
                            yield event_ss({"type": "skills_changed"})
                        # VAPT tools signal "sudo credential required" so the
                        # frontend can pop the centered password dialog.
                        from tools.vapt_tools import translate_sudo_marker
                        needed, reason = translate_sudo_marker(ev.get("output") or "")
                        if needed:
                            yield event_ss({"type": "sudo_password", "reason": reason})
                        # CODE skill signals "GitHub token required" so the
                        # frontend can pop the centered token dialog.
                        from tools.code_tools import translate_github_marker
                        needed, reason = translate_github_marker(ev.get("output") or "")
                        if needed:
                            yield event_ss({"type": "github_token", "reason": reason})
                    elif ev["type"] == "error":
                        error_seen = True
                    elif ev["type"] == "done":
                        usage = ev.get("usage") or {}
                    yield event_ss(ev)
            finally:
                try:
                    provider.close()
                except Exception:
                    pass

            assistant_text = "".join(assistant_parts).strip()
            if store_messages:
                if assistant_text or tool_events:
                    db.add_message(
                        conv["id"], "assistant", assistant_text,
                        {"voice": voice_mode, "error": error_seen, "usage": usage, "tools": tool_events},
                    )
                if error_seen and not assistant_text:
                    db.add_message(conv["id"], "assistant",
                                   "[The assistant hit an error; please retry.]",
                                   {"error": True})

                if config.MEMORY_SUMMARIZE and mem and (assistant_text or user_text):
                    threading.Thread(
                        target=background_summarize,
                        args=(uid, conv["id"], user_text, assistant_text,
                              provider_mgr, provider_name, rt),
                        daemon=True,
                    ).start()
            yield event_ss({"type": "end", "ok": not error_seen})

        resp = Response(stream_gen(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    # ---- Obsidian attachments -------------------------------------------------
    @app.route("/api/obsidian/file", methods=["GET"])
    def obsidian_file():
        if not current_user():
            return jsonify({"error": "Not signed in"}), 401
        from tools.obsidian_tools import _is_md, _resolve, _vault_or_error
        vault, err = _vault_or_error(config)
        if err:
            return jsonify({"error": err}), 400
        rel = request.args.get("path", "").strip()
        if not rel:
            return jsonify({"error": "Missing path"}), 400
        target, err = _resolve(vault, rel)
        if err:
            return jsonify({"error": err}), 400
        if not target.exists() or _is_md(target):
            return jsonify({"error": "Attachment not found or is a note"}), 404
        mtype, _ = mimetypes.guess_type(str(target))
        return send_file(str(target), mimetype=mtype)

    return app


def event_ss(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


def background_summarize(uid, conv_id, user_text, assistant_text, provider_mgr, provider_name, rt):
    """One-shot, non-blocking memory entry: a 1-2 sentence takeaway."""
    mem = get_memory()
    if not mem:
        return
    if not user_text or len(user_text) < 12:
        return
    if assistant_text and len(assistant_text) < 40:
        return
    provider = None
    try:
        model = rt.get("model")
        if not model:
            model = config.CODEX_MODEL if provider_name == "codex" else (
                rt.get("ollama_model") or rt.get("torch_model") or config.DEFAULT_MODEL)
        from models.providers import ProviderManager as PM

        pm = PM(provider_mgr.bearer, provider_mgr.use_oauth_access, rt)
        provider = pm.build(provider_name, model)
        prompt = (
            "Summarize the following exchange in ONE sentence as a durable memory "
            "fact about the user, in the user's language. No preamble.\n\n"
            f"User: {user_text[:400]}\nAssistant: {(assistant_text or '')[:500]}"
        )
        msgs = [{"role": "system", "content": prompt}]
        chunks = list(provider.chat_stream(msgs, None))
        text = "".join(c["content"] for c in chunks if c["type"] == "text").strip()
        if text:
            mem.remember(uid, f"[auto] {text}", category="exchange", conversation_id=conv_id)
    except Exception:
        pass
    finally:
        if provider is not None:
            provider.close()


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    app_ = create_app()
    app_.run(host=config.HOST, port=config.PORT, threaded=True, debug=False)
