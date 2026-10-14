"""REST routes for the audit log.

Read-only and session-scoped like every other APEX surface. Unlike a
conversation or a task, the audit log is a machine-level artifact - it is how a
compromised or misbehaving *tool* is seen at all - so ``scope=all`` exposes the
whole stream. That is consistent with the machine's existing trust boundary:
every authenticated user already has full machine control through
``terminal_command``. Secrets never reach here because the writer redacts before
a line hits disk.

Routes:
  GET  /api/logs            query events (filters + limit + scope)
  GET  /api/logs/stats      log health: files, counts by level/category
  GET  /api/logs/alerts     persisted security findings (scanner + audit)
  GET  /api/logs/audit      run the analyzer on demand over a window
"""
from __future__ import annotations

from flask import jsonify, request

from observability.analyzer import analyze, summarize
from observability.events import parse_since
from observability.store import get_log


def register_log_routes(app, require_user, config, log=None):
    log = log or get_log()
    max_query = max(1, int(getattr(config, "OBSERVABILITY_MAX_QUERY", 500)))

    def _require():
        user = require_user()
        if not user:
            return None, (jsonify({"error": "unauthorized"}), 401)
        return user, None

    def _limit(default: int = 100) -> int:
        try:
            return max(1, min(int(request.args.get("limit", default)), max_query))
        except (TypeError, ValueError):
            return default

    def _owner_scope(user) -> str | None:
        """Return the user_id filter for the requested scope (None = everyone)."""
        scope = (request.args.get("scope") or "all").lower()
        if scope == "self":
            return user["id"]
        return None

    @app.get("/api/logs")
    def list_logs():
        user, err = _require()
        if err:
            return err
        if not log.enabled:
            return jsonify({"enabled": False, "count": 0, "records": []})
        records = log.read(
            limit=_limit(),
            level=request.args.get("level"),
            category=request.args.get("category"),
            event=request.args.get("event"),
            tool=request.args.get("tool"),
            text=request.args.get("text"),
            since=request.args.get("since"),
            user_id=_owner_scope(user),
        )
        return jsonify({"enabled": True, "count": len(records), "records": records})

    @app.get("/api/logs/stats")
    def log_stats():
        user, err = _require()
        if err:
            return err
        return jsonify(log.stats())

    @app.get("/api/logs/alerts")
    def log_alerts():
        user, err = _require()
        if err:
            return err
        if not log.enabled:
            return jsonify({"enabled": False, "count": 0, "alerts": []})
        alerts = log.read(
            limit=_limit(200),
            category="security",
            since=request.args.get("since"),
            text=request.args.get("text"),
        )
        return jsonify({"enabled": True, "count": len(alerts), "alerts": alerts})

    @app.get("/api/logs/audit")
    def log_audit():
        user, err = _require()
        if err:
            return err
        if not log.enabled:
            return jsonify({"enabled": False, "findings": [], "events_scanned": 0})
        since = request.args.get("since")
        if not since:
            try:
                window = int(request.args.get("window", 86400))
            except (TypeError, ValueError):
                window = 86400
            import time

            since = time.time() - max(60, window)
        else:
            since = parse_since(since) or 0
        scope_self = (request.args.get("scope") or "all").lower() == "self"
        records = log.read(
            limit=max(max_query, 5000),
            since=since,
            user_id=user["id"] if scope_self else None,
        )
        findings = analyze(records)
        return jsonify(
            {
                "enabled": True,
                "since": since,
                "events_scanned": len(records),
                "findings": findings,
                "summary": summarize(findings),
            }
        )
