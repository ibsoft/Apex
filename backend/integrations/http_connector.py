"""REST (OpenAPI) and GraphQL (introspection) connectors.

Both execute over ``requests`` with a hard timeout and never follow a redirect
that changes scheme. Discovery pulls a schema once (OpenAPI document, or a
GraphQL introspection query) and caches it via the manager; the per-connection
``credentials`` (api_key / bearer / basic auth) are injected on the way out and
never logged.
"""
from __future__ import annotations

import json
import urllib.parse

import requests

from .errors import ConnectorError

HTTP_TIMEOUT = 15
_MAX_BODY = 4000


class HttpConnectorError(ConnectorError):
    pass


def _headers(creds: dict, extra: dict | None = None) -> dict:
    headers = dict(extra or {})
    headers.setdefault("Accept", "application/json")
    api_key = (creds.get("api_key") or "").strip()
    bearer = (creds.get("bearer_token") or "").strip()
    if bearer:
        if "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {bearer}"
    elif api_key:
        # OpenAPI-style X-API-Key; a per-connection header config overrides.
        headers.setdefault("X-API-Key", api_key)
    basic_user = (creds.get("basic_user") or "").strip()
    basic_pass = (creds.get("basic_password") or "")
    if basic_user:
        import base64

        token = base64.b64encode(f"{basic_user}:{basic_pass}".encode("utf-8")).decode("ascii")
        headers.setdefault("Authorization", f"Basic {token}")
    for key, value in dict(extra or {}).items():
        headers[key] = value
    return headers


def _safe_get(url: str, timeout: int) -> requests.Response:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HttpConnectorError("URL must be http(s)")
    resp = requests.get(url, timeout=timeout, allow_redirects=True, verify=_verify())
    if resp.url and urllib.parse.urlparse(resp.url).scheme not in ("http", "https"):
        raise HttpConnectorError("redirected off http(s)")
    return resp


def _verify() -> bool:
    # connection-level flag: allow disabling TLS verification on private APIs.
    try:
        from config import config

        return not getattr(config, "INTEGRATIONS_TLS_VERIFY_OFF", False)
    except Exception:
        return True


def _body_text(resp: requests.Response) -> str:
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].lower()
    if ctype in ("application/json", "application/problem+json"):
        try:
            return json.dumps(resp.json(), ensure_ascii=False)[:_MAX_BODY]
        except Exception:
            pass
    return (resp.text or "")[:_MAX_BODY]


def _openapi_base_url(openapi: dict, override: str) -> str:
    if override:
        return override.rstrip("/")
    servers = openapi.get("servers") or []
    if servers and servers[0].get("url"):
        return servers[0]["url"].rstrip("/")
    return ""


def openapi_tools(openapi: dict) -> list[dict]:
    """Flatten an OpenAPI document into a tool list the model can call."""
    tools: list[dict] = []
    paths = openapi.get("paths") or {}
    for path, item in paths.items():
        for method, op in item.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            op_id = op.get("operationId") or f"{method.upper()} {path}"
            params = []
            for p in op.get("parameters") or []:
                params.append({
                    "name": p.get("name"),
                    "in": p.get("in"),
                    "required": bool(p.get("required")),
                    "schema": p.get("schema") or {},
                })
            request_body = op.get("requestBody") or {}
            content = request_body.get("content") or {}
            schema_json = None
            if content.get("application/json"):
                schema_json = content["application/json"].get("schema")
            body_fields = _schema_fields(schema_json)
            tools.append({
                "name": op_id,
                "title": op_id,
                "description": (op.get("summary") or op.get("description") or "").strip(),
                "method": method.upper(),
                "path": path,
                "parameters": params,
                "body_fields": body_fields,
                "body_required": bool(request_body.get("required")),
                "security": op.get("security"),
            })
    return tools


def _schema_fields(schema: dict | None, depth: int = 0) -> list[dict]:
    if not schema or depth > 2:
        return []
    fields = []
    schema = schema or {}
    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        for name, prop in props.items():
            fields.append({
                "name": name,
                "type": prop.get("type", "string"),
                "required": name in (schema.get("required") or []),
                "description": prop.get("description") or "",
            })
    elif schema.get("$ref"):
        ref = str(schema["$ref"]).rsplit("/", 1)[-1]
        fields.append({"name": ref, "type": "object", "required": False})
    return fields


class RestConnector:
    def __init__(self, config: dict, creds: dict):
        self.config = config
        self.creds = creds
        self.base = (config.get("base_url") or config.get("url") or "").rstrip("/")
        if not self.base or not urllib.parse.urlparse(self.base).scheme in ("http", "https"):
            raise HttpConnectorError("REST connector needs a valid http(s) base_url")

    def test(self) -> str:
        resp = _safe_get(self.base, HTTP_TIMEOUT)
        return "ok" if resp.status_code < 500 else f"status {resp.status_code}"

    def request(self, tool: dict, arguments: dict) -> dict:
        path = tool.get("path") or ""
        method = (tool.get("method") or "GET").upper()
        url = self.base + path
        params = {}
        body = None
        path_parts = url.split("{")
        if len(path_parts) > 1 and not path_parts[1].startswith("{"):
            # resolve path params like {id}
            for kw in _path_params(tool.get("path") or ""):
                if kw in arguments:
                    url = url.replace(f"{{{kw}}}", str(arguments.pop(kw)))
        for p in tool.get("parameters") or []:
            loc = p.get("in")
            val = arguments.get(p.get("name"))
            if loc == "query" and val is not None:
                params[p["name"]] = val
            if loc == "header" and val is not None:
                pass  # headers handled below for ak/creds
        if method in ("POST", "PUT", "PATCH"):
            body = _pick_body(tool, arguments)
        return self._execute(method, url, params, body)

    def _execute(self, method: str, url: str, params: dict, body) -> dict:
        headers = _headers(self.creds, {
            k: v for k, v in (self.config.get("headers") or {}).items()
        })
        try:
            resp = requests.request(
                method, url, params=params, json=body, timeout=HTTP_TIMEOUT,
                allow_redirects=True, verify=_verify(), headers=headers)
        except requests.RequestException as exc:
            raise HttpConnectorError(f"request failed: {exc}") from exc
        resp_url = resp.url or url
        if urllib.parse.urlparse(resp_url).scheme not in ("http", "https"):
            raise HttpConnectorError("redirected off http(s)")
        return {
            "status": resp.status_code,
            "body": _body_text(resp),
            "content_type": resp.headers.get("Content-Type", ""),
        }

    def discover(self) -> dict:
        """Fetch (or reuse) the OpenAPI document and flatten tools."""
        url = (self.config.get("openapi_url") or self.base).rstrip("/")
        if not self.config.get("openapi_url"):
            candidates = [url, f"{url}/openapi.json", f"{url}/swagger.json",
                          f"{url}/api-docs"]
            doc = None
            for candidate in candidates:
                try:
                    resp = _safe_get(candidate, HTTP_TIMEOUT)
                except HttpConnectorError:
                    continue
                if resp.status_code < 400 and "application/json" in (resp.headers.get("Content-Type") or ""):
                    try:
                        doc = resp.json()
                    except ValueError:
                        continue
                    if doc and "paths" in doc or "operations" in doc:
                        break
            if doc is None:
                raise HttpConnectorError(
                    "no OpenAPI document found; set an explicit openapi_url")
        else:
            resp = _safe_get(url, HTTP_TIMEOUT)
            if resp.status_code >= 400:
                raise HttpConnectorError(f"openapi_url returned {resp.status_code}")
            try:
                doc = resp.json()
            except ValueError as exc:
                raise HttpConnectorError("openapi_url is not JSON") from exc
        tools = openapi_tools(doc)
        api_base = _openapi_base_url(doc, self.config.get("base_url"))
        return {
            "kind": "rest",
            "openapi_version": doc.get("openapi") or doc.get("swagger"),
            "title": doc.get("info", {}).get("title"),
            "endpoints": [{"method": t["method"], "path": t["path"],
                           "name": t["name"],
                           "description": t["description"]} for t in tools],
            "tools": tools,
            "base_url": api_base,
        }


class GraphqlConnector:
    def __init__(self, config: dict, creds: dict):
        self.config = config
        self.creds = creds
        self.url = (config.get("url") or config.get("endpoint") or "").rstrip("/")
        self.base = (config.get("base_url") or "").rstrip("/")
        if not self.url:
            raise HttpConnectorError("GraphQL connector needs an endpoint url")

    def test(self) -> str:
        if self._introspect():
            return "ok"
        return "unreachable"

    def _introspect(self) -> dict | None:
        query = """{ __schema { queryType { name } types { name kind fields { name args { name type { name kind ofType { name kind } } } } } } }"""
        resp = self._post({"query": query})
        data = resp.get("data", {}).get("__schema")
        return data if data else None

    def _post(self, payload: dict) -> dict:
        headers = _headers(self.creds, dict(self.config.get("headers") or {}))
        try:
            resp = requests.post(self.url, json=payload, timeout=HTTP_TIMEOUT,
                                 allow_redirects=True, verify=_verify(), headers=headers)
        except requests.RequestException as exc:
            raise HttpConnectorError(f"GraphQL request failed: {exc}") from exc
        if urllib.parse.urlparse(resp.url or self.url).scheme not in ("http", "https"):
            raise HttpConnectorError("redirected off http(s)")
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpConnectorError(f"GraphQL returned non-JSON ({resp.status_code})") from exc

    def query(self, gql: str, variables: dict | None = None) -> dict:
        result = self._post({"query": gql, "variables": variables or {}})
        return {
            "status": 200,
            "body": json.dumps(result, ensure_ascii=False)[:_MAX_BODY],
            "content_type": "application/json",
        }

    def discover(self) -> dict:
        data = self._introspect()
        if not data:
            raise HttpConnectorError("GraphQL introspection returned nothing")
        types = []
        seen = set()
        for t in data.get("types") or []:
            name = t.get("name") or ""
            kind = t.get("kind") or ""
            if name.startswith("__") or name in seen:
                continue
            seen.add(name)
            fields = []
            for f in t.get("fields") or []:
                fields.append({
                    "name": f.get("name"),
                    "args": [a.get("name") for a in (f.get("args") or [])],
                    "type": _gql_type_str(f.get("type")),
                })
            types.append({"name": name, "kind": kind, "fields": fields})
        return {
            "kind": "graphql",
            "endpoint": self.url,
            "query_type": (data.get("queryType") or {}).get("name"),
            "types": [t for t in types if t["kind"] in ("OBJECT", "INTERFACE")],
        }


def _gql_type_str(spec: dict | None) -> str:
    if not spec:
        return "?"
    kind = spec.get("kind") or ""
    name = spec.get("name")
    of = _gql_type_str(spec.get("ofType"))
    base = name or of
    if kind == "NON_NULL":
        return base + "!"
    if kind == "LIST":
        return f"[{base}]"
    return base


def _path_params(path: str) -> list[str]:
    import re

    return re.findall(r"\{([^}]+)\}", path)


def _pick_body(tool: dict, arguments: dict) -> dict:
    body = {}
    for f in tool.get("body_fields") or []:
        name = f.get("name")
        if name in arguments:
            body[name] = arguments[name]
    # Also accept a caller-provided "body" passthrough.
    if "body" in arguments and isinstance(arguments["body"], dict):
        body.update(arguments["body"])
    if not body and any(k in arguments for k in ("query", "mutation", "variables")):
        body = arguments
    return body or None


def build_http_connector(kind: str, config: dict, creds: dict):
    if kind == "rest":
        return RestConnector(config, creds)
    if kind == "graphql":
        return GraphqlConnector(config, creds)
    raise HttpConnectorError(f"unsupported http kind `{kind}`")