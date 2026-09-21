"""Adapt Codex app-server streaming and dynamic tools to APEX's agent loop."""
import json

from codex_bridge import CodexClient, CodexError
from config import config


class CodexProvider:
    kind = "codex"

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        self.pending = None
        self.allowed_tools = set()

    def client_options(self):
        raise CodexError("The Codex provider uses the responses engine. Select that engine in Settings.")

    def chat_stream(self, messages, tools=None):
        if self.client is None:
            self.client = CodexClient(config.CODEX_HOME, config.CODEX_BINARY)
            account = self.client.account()
            if not account or account.get("type") != "chatgpt":
                raise CodexError("Sign in with ChatGPT using python backend/setup_provider.py first.")
            definitions = [tool["function"] for tool in tools or [] if tool.get("type") == "function"]
            self.allowed_tools = {tool["name"] for tool in definitions}
            system = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
            thread = self.client.request("thread/start", {
                "model": self.cfg.model or None,
                "modelProvider": "openai",
                "cwd": self.client.workspace.name,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "ephemeral": True,
                "baseInstructions": system,
                "developerInstructions": "You are APEX. Use only the supplied APEX dynamic tools. "
                                         "The user input contains the conversation history as JSON; answer its latest user message.",
                "dynamicTools": [{"type": "function", "name": tool["name"],
                                  "description": tool.get("description", ""),
                                  "inputSchema": tool.get("parameters", {"type": "object", "properties": {}})}
                                 for tool in definitions],
            })
            self.thread_id = thread["thread"]["id"]
            history = [m for m in messages if m.get("role") != "system"]
            self.client.request("turn/start", {"threadId": self.thread_id,
                                               "input": [{"type": "text", "text": json.dumps(history)}]})
        elif self.pending:
            request_id, call_id = self.pending
            result = next((m for m in reversed(messages)
                           if m.get("role") == "tool" and m.get("tool_call_id") == call_id), None)
            if result is None:
                raise CodexError("Missing APEX tool result for the pending Codex request.")
            content = result.get("content", "")
            self.client.send({"id": request_id, "result": {
                "success": True, "contentItems": [{"type": "inputText", "text":
                    content if isinstance(content, str) else json.dumps(content)}]}})
            self.pending = None
        else:
            raise CodexError("This Codex turn has already completed.")
        while True:
            event = self.client.event()
            method, params = event.get("method"), event.get("params", {})
            if method == "item/agentMessage/delta":
                yield {"type": "text", "content": params.get("delta", "")}
            elif method == "item/tool/call" and "id" in event:
                if params.get("tool") not in self.allowed_tools:
                    self.client.send({"id": event["id"], "result": {"success": False,
                                      "contentItems": [{"type": "inputText", "text": "Unknown APEX tool."}]}})
                    continue
                self.pending = (event["id"], params["callId"])
                yield {"type": "tool_calls", "calls": [{"id": params["callId"], "name": params["tool"],
                                                         "arguments": json.dumps(params.get("arguments", {}))}]}
                return
            elif "id" in event and method:
                # APEX does not grant Codex shell/file approvals or interactive requests.
                self.client.send({"id": event["id"], "error": {"code": -32601,
                                  "message": "This interaction is not supported by APEX."}})
            elif method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("status") != "completed":
                    detail = _error_detail(turn) or "Codex turn failed or was interrupted."
                    raise CodexError(f"{detail} Check account limits and model access.")
                return
            elif method == "error" and not params.get("willRetry", False):
                detail = _error_detail(params) or "Codex reported an error."
                raise CodexError(f"{detail} Check your account limits and selected model.")

    def close(self):
        if self.client:
            self.client.close()
            self.client = None


def _error_detail(value):
    """Extract a useful, non-secret message from an app-server error payload."""
    if not isinstance(value, dict):
        return ""
    for key in ("message", "error", "reason", "detail"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            nested = _error_detail(item)
            if nested:
                return nested
    return ""
