"""A real MCP server used by the integration tests.

Built on the official SDK (``mcp.server.MCPServer``) so the tests exercise the
actual protocol: the client spawns this as a subprocess over stdio, initializes,
lists tools/resources/prompts and calls them. Nothing here is mocked.

Run standalone:  python tests/support/mcp_demo_server.py
"""
from __future__ import annotations

import sys

from mcp.server import MCPServer

server = MCPServer(name="apex-demo", version="1.0")


@server.tool(description="Add two integers.")
def add(a: int, b: int) -> int:
    return a + b


@server.tool(description="Greet someone by name.")
def greet(name: str) -> str:
    return f"hello {name}"


@server.tool(description="The server's canned status.")
def status() -> str:
    return "demo server ok"


@server.resource("demo://readme", description="A demo resource")
def readme() -> str:
    return "this is the apex demo resource"


@server.prompt(description="A demo greeting prompt")
def greeting(name: str) -> str:
    return f"Please greet {name} warmly."


if __name__ == "__main__":
    if "--stdio" in sys.argv or len(sys.argv) == 1:
        server.run("stdio")
    else:
        server.run(sys.argv[1])