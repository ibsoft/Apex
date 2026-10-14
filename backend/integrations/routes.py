"""REST routes for the MCP tab.

Owner-scoped like every other APEX resource: the session is the only source of
identity, another user's connection answers 404 (never 403), and secrets are
masked on the way out (``credentials_set`` is a list of names, never values).

Routes:
  GET    /api/integrations                      list (with health)
  POST   /api/integrations                      create
  GET    /api/integrations/kinds                catalogue for the form
  GET    /api/integrations/<id>                 one connection
  PATCH  /api/integrations/<id>                 update
  DELETE /api/integrations/<id>                 delete
  POST   /api/integrations/<id>/test            test now
  POST   /api/integrations/<id>/discover        refresh discovery
  POST   /api/integrations/<id>/query           run an explicit operation
  POST   /api/integrations/<id>/ask             natural-language operation
  GET    /api/integrations/<id>/audit           recent audit entries
  GET    /api/integrations/audit                recent audit, all connections
"""
from __future__ import annotations

from flask import jsonify, request

from .errors import ConnectorError
from .manager import IntegrationManager
from .store import KINDS, kind_secret_fields


def _public(record: dict, manage: IntegrationManager) -> dict:
    out = dict(record)
    out["health"] = manage.health(record)
    # The per-connection system prompt is operator-authored text, not a secret:
    # return it so the edit form can round-trip it.
    out["system_prompt"] = record.get("system_prompt") or ""
    credentials_set = record.get("credentials_set")
    if credentials_set is None:
        decrypted = record.get("credentials_decrypted") or {}
        credentials_set = [k for k, v in sorted(decrypted.items())
                           if str(v or "").strip()]
    out["credentials_set"] = credentials_set
    out.pop("credentials_decrypted", None)
    out.pop("discovery_cache", None)
    return out


def register_integration_routes(app, require_user, config, manage: IntegrationManager):
    from .nl_query import translate

    def _load(connection_id: str):
        user = require_user()
        if not user:
            return None, (jsonify({"error": "unauthorized"}), 401)
        record = manage.store.get(connection_id, user["id"])
        if record is None:
            return None, (jsonify({"error": "not found"}), 404)
        return (user, record), None

    @app.get("/api/integrations/kinds")
    def integration_kinds():
        if not require_user():
            return jsonify({"error": "unauthorized"}), 401
        catalogue = []
        groups = {
            "mcp": ["mcp"],
            "sql": ["postgres", "mysql", "sqlserver", "sqlite", "oracle"],
            "http": ["rest", "graphql"],
        }
        for group, kinds in groups.items():
            for kind in kinds:
                if kind not in KINDS:
                    continue
                entry = {"kind": kind, "group": group,
                         "secrets": list(kind_secret_fields(kind))}
                if kind == "mcp":
                    # The connection kind is `mcp`; the transport is a config
                    # field the form offers.
                    entry["transports"] = ["stdio", "http", "sse"]
                catalogue.append(entry)
        return jsonify({"kinds": catalogue,
                        "enabled": bool(getattr(config, "INTEGRATIONS_ENABLED", False))})

    @app.get("/api/integrations")
    def integration_list():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"connections": [_public(r, manage)
                                        for r in manage.store.list(user["id"])]})

    @app.get("/api/integrations/audit")
    def integration_audit_all():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        limit = int(request.args.get("limit", 50))
        return jsonify({"audit": manage.audit_for_user(user["id"], None, limit)})

    @app.post("/api/integrations")
    def integration_create():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in KINDS:
            return jsonify({"error": f"unknown kind `{kind}`"}), 400
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "a name is required"}), 400
        try:
            record = manage.store.create(
                owner_id=user["id"],
                kind=kind,
                name=name,
                description=str(data.get("description") or ""),
                config=dict(data.get("config") or {}),
                credentials=_clean_credentials(kind, data.get("credentials") or {}),
                system_prompt=str(data.get("system_prompt") or ""),
                enabled=bool(data.get("enabled", True)),
                readonly=bool(data.get("readonly", True)),
                allowed_tables=_list(data.get("allowed_tables")),
                denied_tables=_list(data.get("denied_tables")),
                allowed_operations=_list(data.get("allowed_operations")) or ["select"],
                max_rows=int(data.get("max_rows") or config.INTEGRATIONS_DEFAULT_MAX_ROWS),
                timeout_seconds=int(data.get("timeout_seconds")
                                    or config.INTEGRATIONS_QUERY_TIMEOUT),
                skills=_list(data.get("skills")),
            )
        except Exception as exc:
            return jsonify({"error": f"could not save connection: {exc}"}), 400
        return jsonify({"connection": _public(record, manage)}), 201

    @app.get("/api/integrations/<connection_id>")
    def integration_get(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        return jsonify({"connection": _public(record, manage)})

    @app.patch("/api/integrations/<connection_id>")
    def integration_update(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        data = request.get_json(silent=True) or {}
        patch = _update_patch(data)
        if patch.get("credentials"):
            patch["credentials"] = _clean_credentials(record["kind"], patch["credentials"])
        updated = manage.store.update(connection_id, user["id"], patch)
        if updated is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"connection": _public(updated, manage)})

    @app.delete("/api/integrations/<connection_id>")
    def integration_delete(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        if not manage.store.delete(connection_id, user["id"]):
            return jsonify({"error": "not found"}), 404
        manage._discovery_cache.pop(connection_id, None)
        return jsonify({"deleted": connection_id})

    @app.post("/api/integrations/<connection_id>/test")
    def integration_test(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        return jsonify({"health": manage.test(record)})

    @app.post("/api/integrations/<connection_id>/discover")
    def integration_discover(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        data = request.get_json(silent=True) or {}
        try:
            discovered = manage.discover(record, force=bool(data.get("force", True)))
        except ConnectorError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"discovery": discovered})

    @app.post("/api/integrations/<connection_id>/query")
    def integration_query(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        data = request.get_json(silent=True) or {}
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {k: data[k] for k in
                       ("sql", "tool", "arguments", "query", "variables",
                        "resource", "prompt") if k in data}
        result = manage.query(record, payload)
        status = 200 if not result.get("error") else 400
        return jsonify({"result": result}), status

    @app.post("/api/integrations/<connection_id>/ask")
    def integration_ask(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        data = request.get_json(silent=True) or {}
        request_text = str(data.get("request") or "").strip()
        if not request_text:
            return jsonify({"error": "empty request"}), 400
        provider = _build_provider(app, user, config)
        if provider is None:
            return jsonify({"error": "no model provider is configured"}), 503
        result = translate(manage, record, request_text, provider,
                           execute=bool(data.get("execute", True)))
        status = 200 if not result.get("error") else 400
        return jsonify({"result": result}), status

    @app.get("/api/integrations/<connection_id>/audit")
    def integration_audit(connection_id):
        loaded, error = _load(connection_id)
        if error:
            return error
        user, record = loaded
        limit = int(request.args.get("limit", 50))
        return jsonify({"audit": manage.audit_for_user(user["id"], connection_id, limit)})

    return manage


def _build_provider(app, user, config):
    """Build the same provider the chat engine uses, or None when unconfigured."""
    try:
        from auth import bearer_for_api, is_subscription_access
        from models.providers import ProviderError, ProviderManager
    except Exception:
        return None
    from flask import session

    runtime = session.get("runtime") or {}
    try:
        provider = ProviderManager(
            bearer=bearer_for_api(user["id"]),
            use_oauth_access=is_subscription_access(user["id"]),
            runtime=runtime,
        ).build((runtime.get("provider") or config.PROVIDER_DEFAULT).lower(), None)
        return provider
    except ProviderError:
        return None
    except Exception:
        app.logger.exception("integration provider unavailable")
        return None


def _list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _clean_credentials(kind: str, credentials: dict) -> dict:
    allowed = set(kind_secret_fields(kind))
    out = {}
    for key, value in (credentials or {}).items():
        if key in allowed and value not in (None, ""):
            out[key] = str(value)
    return out


def _update_patch(data: dict) -> dict:
    patch = {}
    for key in ("name", "description", "config", "system_prompt", "enabled",
                "readonly", "allowed_tables", "denied_tables",
                "allowed_operations", "max_rows", "timeout_seconds", "skills"):
        if key in data:
            patch[key] = data[key]
    if isinstance(data.get("credentials"), dict):
        patch["credentials"] = data["credentials"]
    if isinstance(data.get("credentials_clear"), list):
        patch["credentials_clear"] = data["credentials_clear"]
    return patch