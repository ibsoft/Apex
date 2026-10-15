"""REST surface for the AGENTS tab.

Owner-scoped like the rest of the API: the session is the only source of
identity, a sub-agent belonging to somebody else answers 404 (never 403), and
every mutation goes through :class:`SubAgentManager` so validation happens in one
place. A run is started here (Run Now) exactly as the model starts one through
``subagent_delegate`` - the same manager call.
"""
from __future__ import annotations

from flask import jsonify, request

from .manager import SubAgentError, get_manager, subagent_context_block


def register_subagent_routes(app, require_user, config) -> None:
    manager = get_manager(config)

    def _owned(agent_id):
        """Return ``(user, agent)`` or a Flask response tuple on failure."""
        user = require_user()
        if not user:
            return None, (jsonify({"error": "unauthorized"}), 401)
        agent = manager.db.get_subagent(agent_id, user["id"])
        if agent is None:
            return None, (jsonify({"error": "sub-agent not found"}), 404)
        return (user, agent), None

    @app.get("/api/subagents")
    def list_subagents_route():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        agents = [manager.public(a) for a in manager.db.list_subagents(user["id"])]
        return jsonify({
            "agents": agents,
            "enabled": True,
        })

    @app.get("/api/subagents/tools")
    def list_subagent_tools_route():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"tools": manager.known_tool_names()})

    @app.post("/api/subagents")
    def create_subagent_route():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            agent = manager.create(user["id"], data)
        except SubAgentError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"agent": agent}), 201

    @app.get("/api/subagents/<agent_id>")
    def get_subagent_route(agent_id):
        loaded, error = _owned(agent_id)
        if error:
            return error
        user, agent = loaded
        return jsonify({"agent": manager.public(agent, with_runs=True)})

    @app.patch("/api/subagents/<agent_id>")
    def update_subagent_route(agent_id):
        loaded, error = _owned(agent_id)
        if error:
            return error
        user, _ = loaded
        data = request.get_json(silent=True) or {}
        try:
            agent = manager.update(agent_id, user["id"], data)
        except SubAgentError as exc:
            return jsonify({"error": str(exc)}), 400
        if agent is None:
            return jsonify({"error": "sub-agent not found"}), 404
        return jsonify({"agent": agent})

    @app.delete("/api/subagents/<agent_id>")
    def delete_subagent_route(agent_id):
        loaded, error = _owned(agent_id)
        if error:
            return error
        user, _ = loaded
        if not manager.delete(agent_id, user["id"]):
            return jsonify({"error": "sub-agent not found"}), 404
        return jsonify({"ok": True})

    @app.get("/api/subagents/<agent_id>/runs")
    def list_subagent_runs_route(agent_id):
        loaded, error = _owned(agent_id)
        if error:
            return error
        _user, agent = loaded
        runs = manager.db.list_subagent_runs(agent_id, 50)
        return jsonify({"runs": [manager.public_run(r) for r in runs]})

    @app.post("/api/subagents/<agent_id>/run")
    def run_subagent_route(agent_id):
        loaded, error = _owned(agent_id)
        if error:
            return error
        user, _agent = loaded
        data = request.get_json(silent=True) or {}
        prompt = str(data.get("prompt") or "").strip()
        run, err = manager.run_now(agent_id, user["id"], prompt, background=True)
        if run is None:
            return jsonify({"error": err or "could not start the run"}), 400
        return jsonify({"run": manager.public_run(run)}), 202

    @app.post("/api/subagents/ack")
    def ack_subagent_runs_route():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        ids = data.get("ids")
        if not (isinstance(ids, list) and ids):
            ids = None
        cleared = manager.db.mark_subagent_runs_read(user["id"], ids)
        return jsonify({"ok": True, "cleared": cleared})

    @app.get("/api/subagents/context")
    def subagent_context_route():
        """Debug/inspection of exactly what the model is handed about the
        operator's sub-agents (never a secret; reports only)."""
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"context": subagent_context_block(user["id"], manager.db)})
