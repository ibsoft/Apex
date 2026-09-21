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
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_file, session

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
from db import get_db
from memory.store import get_memory
from models.embedders import EmbeddingManager
from models.providers import ProviderError, ProviderManager
from skills.manager import get_skill_manager
from tools.memory_tools import memory_prompt_block


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    )

    # ---- CORS ---------------------------------------------------------------
    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin") or config.FRONTEND_URL
        if origin == "null":
            origin = config.FRONTEND_URL
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return resp

    @app.before_request
    def handle_preflight():
        if request.method == "OPTIONS":
            return ("", 204)

    # ---- helpers ------------------------------------------------------------
    def current_user():
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
        except Exception:
            app.logger.exception("dev auto-login failed")

    def require_user():
        uid = current_user()
        if not uid:
            return None
        row = get_db().get_user(uid)
        return row

    def runtime(dotted: bool = False):
        """Effective runtime settings: DB overrides merged over env defaults."""
        return get_db().all_settings()

    def engines_available():
        try:
            return config.agent_engines_available
        except Exception:
            return ["responses"]

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
    @app.get("/api/oauth/start")
    def oauth_start():
        from auth import OAuthSession

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
        intent = session.pop("apex_intent", None) or config.FRONTEND_URL
        return redirect(intent)

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
                "settings": rt,
                "engine": rt.get("engine") or config.AGENT_ENGINE,
                "provider": rt.get("provider") or config.PROVIDER_DEFAULT,
            }
        )

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "oauth_configured": config.oauth_configured,
                "engines": engines_available(),
            }
        )

    @app.get("/api/logout")
    def logout():
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
                "memory_enabled": bool(mem),
                "embedding": (get_memory().embedding_name if mem else None),
                "wake_word": rt.get("wake_word") or config.WAKE_WORD,
                "follow_up_seconds": int(rt.get("follow_up_seconds") or config.FOLLOW_UP_SECONDS),
                "voice": rt.get("voice") or config.VOICE,
                "response_language": rt.get("response_language") or config.RESPONSE_LANGUAGE,
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

    # ---- settings --------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(
            {
                "settings": runtime(),
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
            "base_url", "torch_model", "model_extra",
        }
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
            get_db().set_setting(key, value)
        get_skill_manager().refresh()
        return jsonify({"ok": True, "settings": runtime()})

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

    @app.delete("/api/conversations/<conv_id>")
    def delete_convo(conv_id: str):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        conv = get_db().get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            return jsonify({"error": "not found"}), 404
        get_db().delete_conversation(conv_id)
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

        # resolve conversation
        conv_id = data.get("conversation_id") or ""
        conv = db.get_conversation(conv_id) if conv_id else None
        if conv and conv["user_id"] != uid:
            return jsonify({"error": "not allowed"}), 403
        if conv is None:
            title = user_text[:48] + ("…" if len(user_text) > 48 else "")
            conv = db.create_conversation(uid, title=title)

        skill_name = data.get("skill") or conv["skill"] or "general"
        db.update_conversation(conv["id"], skill=skill_name)

        engine_name = (rt.get("engine") or config.AGENT_ENGINE).lower()
        provider_name = (rt.get("provider") or config.PROVIDER_DEFAULT).lower()
        if provider_name == "codex":
            engine_name = "responses"
        voice_mode = bool(data.get("voice_mode", False))

        # persist user message
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

        # resolve model per provider
        model = data.get("model") or rt.get("model") or skill_model
        if not model:
            if provider_name == "codex":
                model = config.CODEX_MODEL
            elif provider_name == "ollama":
                model = rt.get("ollama_model") or config.OLLAMA_MODEL
            elif provider_name == "kimi":
                model = rt.get("kimi_model") or config.KIMI_MODEL
            elif provider_name == "torch":
                model = rt.get("torch_model") or config.TORCH_MODEL
            elif is_subscription_access(uid):
                model = config.CHATGPT_MODEL
            else:
                model = config.DEFAULT_MODEL

        try:
            provider = provider_mgr.build(provider_name, model)
        except ProviderError as exc:
            return jsonify({"error": str(exc)}), 502

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
        )

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
            skill_tools=list(get_skill_manager().select(skill_name).tools),
            memory=mem,
            runtime=rt,
            voice_mode=voice_mode,
            user_name=session.get("name") or user.get("name") or "",
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
            error_seen = False
            usage = {}
            try:
                for ev in engine.stream():
                    if ev["type"] == "text_delta":
                        assistant_parts.append(ev["content"])
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
            if assistant_text:
                db.add_message(
                    conv["id"], "assistant", assistant_text,
                    {"voice": voice_mode, "error": error_seen, "usage": usage},
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