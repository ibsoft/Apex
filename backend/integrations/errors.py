"""Shared error types for the integrations package."""


class ConnectorError(Exception):
    """A plain, user-facing reason (never a traceback)."""


class McpError(ConnectorError):
    """An MCP transport/session failure, also user-facing."""