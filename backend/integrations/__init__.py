"""Universal Integration Manager.

Connections to MCP servers, SQL databases (PostgreSQL, MySQL, SQL Server,
SQLite, Oracle), REST/GraphQL APIs and remote services, managed from the MCP tab
in the UI and queryable by the agent through ``integration_query`` and friends.

Layout:

* ``store.py``      - SQLite persistence of connections + the audit log, encrypted
* ``secrets.py``    - Fernet encryption of stored credentials
* ``dialects.py``   - SQL lexer / strict validator (read-only by default)
* ``connectors.py`` - database connectors + watchdog query timeout
* ``mcp_client.py`` - MCP client over the official SDK (stdio / http / sse)
* ``http_connector.py`` - REST (OpenAPI) and GraphQL (introspection) connectors
* ``nl_query.py``   - natural language -> validated SQL via the agent provider
* ``manager.py``    - facade: CRUD, test, health cache, per-skill prompt blocks
* ``tools.py``      - agent tool wrappers (integration_query & friends)
* ``routes.py``     - REST endpoints for the MCP tab
"""