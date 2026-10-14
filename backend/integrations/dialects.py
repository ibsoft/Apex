"""Strict SQL validation for the integration connectors.

This is the security boundary between "natural language becomes SQL" and "that
SQL runs". It is deliberately conservative and grammar-independent - no single
SQL parser exists for five dialects - so it works on tokens:

* at most one statement (a trailing semicolon is the only one allowed);
* the statement must be one the connection's operator has granted
  (read-only default means SELECT / WITH...SELECT only);
* every referenced table must pass the connection's allow / deny lists;
* a handful of filesystem/escape hatches (``writefile``, ``load_extension``,
  ``INTO OUTFILE`` ...) are never allowed in any mode.

The validator does not need to understand every dialect's grammar perfectly -
it needs to reject anything that is not a plain, allowlisted, read-only query.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ConnectorError

_DIALECT_ALIASES = {
    "sqlite": "sqlite",
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlserver": "mssql",
    "oracle": "oracle",
    "rest": "sqlite",
    "graphql": "sqlite",
}


def dialect_for(kind: str) -> str:
    return _DIALECT_ALIASES.get(kind, "sqlite")


# Every statement family other than reads (or an explicitly granted mutation).
_WRITE_AND_ADMIN_KEYWORDS = frozenset({
    "CREATE", "DROP", "ALTER", "TRUNCATE", "REPLACE", "RENAME", "GRANT",
    "REVOKE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "VACUUM",
    "REINDEX", "ANALYZE", "ATTACH", "DETACH", "MERGE", "UPSERT", "COPY",
    "CALL", "EXEC", "EXECUTE", "PREPARE", "DEALLOCATE", "LANGUAGE",
})

_MUTATIONS = frozenset({"INSERT", "UPDATE", "DELETE"})

_SELECT_KEYWORDS = frozenset({"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "PRAGMA"})

# Identifiers that always follow a table reference.
_FROM_KEYWORDS = frozenset({"FROM", "JOIN", "INNER", "LEFT", "RIGHT", "FULL",
                            "CROSS", "OUTER", "INTO", "UPDATE", "TABLE"})

_JOIN_KINDS = frozenset({"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER"})

# SQL functions that reach at the filesystem / process; never allowed.
_BLOCKED_FUNCTIONS = frozenset({
    "WRITEFILE", "READFILE", "LOAD_EXTENSION", "PG_READ_FILE",
    "PG_SLEEP", "BENCHMARK", "SLEEP",
})

# ``INTO OUTFILE`` (MySQL) writes a file; ``INTO DUMPFILE`` too.
_BLOCKED_FRAGMENTS = (
    ("INTO", "OUTFILE"),
    ("INTO", "DUMPFILE"),
    ("OUTFILE",),
    ("DUMPFILE",),
)

_MAX_SQL_CHARS = 20_000


class ValidationError(ConnectorError):
    """Raised when a query does not pass the strict validator."""


@dataclass
class Token:
    kind: str            # word | ident | string | number | op | comment
    value: str           # raw text
    upper: str = ""      # value.upper() for words


@dataclass
class ValidatedQuery:
    sql: str
    operation: str                       # select | insert | update | delete
    tables: list[str] = field(default_factory=list)
    complexity: int = 0


def tokenize(sql: str) -> list[Token]:
    """Split SQL into tokens, respecting quoted identifiers, strings and
    comments. Dialect-agnostic: all four identifier quote styles are handled."""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            tokens.append(Token("comment", sql[i: n if j < 0 else j]))
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            tokens.append(Token("comment", sql[i: n if j < 0 else j + 2]))
            i = n if j < 0 else j + 2
            continue
        if c in "'\"`[":
            if c == "'":
                j, end = _scan_quoted(sql, i, "'")
                tokens.append(Token("string", sql[i:j + 1]))
                i = j + 1
                continue
            if c == "`":
                j, _ = _scan_quoted(sql, i, "`")
                tokens.append(Token("ident", sql[i:j + 1], sql[i:j + 1][1:-1].upper()))
                i = j + 1
                continue
            if c == '"':
                # Double quotes are identifiers in standard SQL and PG.
                j, _ = _scan_quoted(sql, i, '"')
                tokens.append(Token("ident", sql[i:j + 1], sql[i:j + 1][1:-1].upper()))
                i = j + 1
                continue
            if c == "[":
                j = sql.find("]", i + 1)
                if j < 0:
                    j = n - 1
                tokens.append(Token("ident", sql[i:j + 1], sql[i + 1:j].upper()))
                i = j + 1
                continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "._'"):
                j += 1
            tokens.append(Token("number", sql[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_" or c == "@" or c == "#" or c == "$":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_@#$"):
                j += 1
            value = sql[i:j]
            tokens.append(Token("word", value, value.upper()))
            i = j
            continue
        # operators / punctuation
        tokens.append(Token("op", c, c.upper()))
        i += 1
    return tokens


def _scan_quoted(sql: str, start: int, quote: str) -> tuple[int, str]:
    j = start + 1
    n = len(sql)
    while j < n:
        if sql[j] == quote:
            if j + 1 < n and sql[j + 1] == quote:  # doubled escape
                j += 2
                continue
            if quote == "'" and sql[j] == "'":
                return j, ""
            return j, ""
        j += 1
    return n - 1, ""


def normalize_table(name: str) -> str:
    """Strip quotes and a possible schema prefix, lower-case for matching."""
    cleaned = name.strip()
    for c in ("`", "'", '"'):
        cleaned = cleaned.replace(c, "")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    # schema.table -> table (the schema is the qualifier, the table is what the
    # allowlist names)
    if "." in cleaned:
        cleaned = cleaned.rsplit(".", 1)[-1]
    return cleaned.lower()


def validate(
    sql: str,
    *,
    kind: str = "sqlite",
    readonly: bool = True,
    allowed_operations: list[str] | None = None,
    allowed_tables: list[str] | None = None,
    denied_tables: list[str] | None = None,
) -> ValidatedQuery:
    """Validate ``sql`` and return a :class:`ValidatedQuery` or raise
    :class:`ValidationError`."""
    if not sql or not sql.strip():
        raise ValidationError("empty query")
    if len(sql) > _MAX_SQL_CHARS:
        raise ValidationError(f"query exceeds {_MAX_SQL_CHARS} characters")

    dialect = dialect_for(kind)
    ops = {str(o).strip().lower() for o in (allowed_operations or ["select"])}
    allow = {normalize_table(t) for t in (allowed_tables or [])}
    deny = {normalize_table(t) for t in (denied_tables or [])}

    tokens = [t for t in tokenize(sql) if t.kind != "comment"]
    if not tokens:
        raise ValidationError("empty query")

    # Exactly one statement. A single trailing semicolon is the only one allowed.
    meaningful = [t for t in tokens if t.kind != "op" or t.value != ";"]
    semicolons = [t for t in tokens if t.kind == "op" and t.value == ";"]
    if semicolons:
        if len(semicolons) > 1:
            raise ValidationError("multiple statements are not allowed")
        if semicolons[0] is not tokens[-1]:
            raise ValidationError("multiple statements are not allowed")
    tokens = meaningful

    # The operation is the first significant keyword (a leading WITH makes it a
    # CTE but the terminal statement still dictates the operation).
    keyword = _leading_keyword(tokens, dialect)
    if keyword in _WRITE_AND_ADMIN_KEYWORDS or keyword in _MUTATIONS:
        operation = keyword.lower()
        if operation == "with":
            # WITH ... INSERT/UPDATE/DELETE: find the write keyword after the CTE.
            key = _terminal_keyword(tokens)
            if key in _WRITE_AND_ADMIN_KEYWORDS or key in _MUTATIONS:
                operation = key.lower()
            else:
                operation = "select"
    elif keyword in _SELECT_KEYWORDS or keyword == "WITH":
        operation = "select"
    else:
        raise ValidationError(f"unsupported statement starting with `{keyword or '?'}`")

    if not readonly and "select" not in ops:
        ops.add("select")
    if operation not in ops:
        raise ValidationError(
            f"`{operation.upper()}` is not permitted on this connection "
            f"(allowed: {', '.join(sorted(ops)) or 'none'})")

    # Never allow these functions whatever the grants say.
    for token in tokens:
        if token.kind == "word" and token.upper in _BLOCKED_FUNCTIONS:
            raise ValidationError(f"`{token.upper}()` is blocked")

    # INTO OUTFILE / INTO DUMPFILE (MySQL writes).
    for index, token in enumerate(tokens):
        if token.kind != "word":
            continue
        if token.upper == "INTO" and index + 1 < len(tokens) and tokens[index + 1].upper in ("OUTFILE", "DUMPFILE"):
            raise ValidationError("file-writing INTO ... OUTFILE is blocked")
        if token.upper in ("OUTFILE", "DUMPFILE"):
            raise ValidationError("file-writing INTO ... OUTFILE is blocked")

    # Table references that matter.
    tables = _extract_tables(tokens, dialect)
    seen = set()
    for table in tables:
        norm = normalize_table(table)
        if not norm:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        if deny and norm in deny:
            raise ValidationError(f"table `{norm}` is denied on this connection")
        if allow and norm not in allow:
            raise ValidationError(
                f"table `{norm}` is not permitted on this connection "
                f"(allowed: {', '.join(sorted(allow)) or 'none'})")

    return ValidatedQuery(sql=sql.strip(), operation=operation, tables=sorted(seen),
                          complexity=len(tokens))


def _leading_keyword(tokens: list[Token], dialect: str) -> str:
    for token in tokens:
        if token.kind == "word":
            return token.upper
    return ""


def _terminal_keyword(tokens: list[Token]) -> str:
    depth = 0
    found: list[tuple[int, str]] = []
    for i, token in enumerate(tokens):
        if token.kind == "op":
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
        elif token.kind == "word" and depth == 0:
            found.append((i, token.upper))
    # After the WITH clause the first depth-0 keyword after the closing of the
    # CTE bodies is the terminal statement; pick the keyword that follows the
    # last ')', i.e. the one closest to a primary keyword set.
    for i, up in found:
        if up in _MUTATIONS or up in _WRITE_AND_ADMIN_KEYWORDS or up in _SELECT_KEYWORDS:
            return up
    return found[-1][1] if found else ""


def _extract_tables(tokens: list[Token], dialect: str) -> list[str]:
    """Collect table references. Handles FROM/JOIN chains with aliases and
    comma lists, at any paren depth (subqueries included)."""
    tables: list[str] = []
    cte_names: set[str] = set()
    i = 0
    pending: str | None = None
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind == "word":
            up = tok.upper
            if up == "WITH" and i + 1 < len(tokens):
                # collect CTE names: WITH name AS ... the names are not tables.
                j = i + 1
                if tokens[j].kind == "word":
                    cte_names.add(tokens[j].value.lower())
                    i = j
                    continue
            if up in _FROM_KEYWORDS or up in _JOIN_KINDS:
                pending = up
                i += 1
                continue
            if pending:
                if up in ("AS", "ON", "USING", "WHERE", "GROUP", "ORDER",
                          "HAVING", "LIMIT", "OFFSET", "UNION", "INTERSECT",
                          "EXCEPT", "AND", "OR", "NOT", "WHEN", "THEN", "ELSE",
                          "END", "CASE", "LEFT", "RIGHT", "FULL", "INNER",
                          "CROSS", "OUTER", "JOIN", "LATERAL", "IN", "SELECT",
                          "SET", "RETURNING"):
                    pending = None
                    continue
                # A quoted pair after the pending keyword: schema or table.
                table = tok.value
                if i + 1 < len(tokens) and tokens[i + 1].kind == "op" and tokens[i + 1].value == ".":
                    q = tokens[i + 2] if i + 2 < len(tokens) else None
                    if q and q.kind in ("word", "ident"):
                        table = (table + "." + q.value)
                        i += 2
                if table.lower() not in cte_names:
                    tables.append(table)
                pending = None
                i += 1
                continue
            if up in _SELECT_KEYWORDS:
                i += 1
                continue
            # comma list after FROM: prev op ',' and we are still accumulating
            i += 1
            continue
        elif tok.kind == "op" and tok.value == "," and pending:
            # FROM a, b - the next word after the comma is another table.
            pass
        i += 1
    return tables


def has_limit(sql: str, kind: str = "sqlite") -> bool:
    tester = {
        "sqlite": lambda up: up == "LIMIT",
        "postgres": lambda up: up == "LIMIT",
        "mysql": lambda up: up == "LIMIT",
        "mssql": lambda up: up == "TOP",
        "oracle": lambda up: up in ("LIMIT", "FETCH", "ROWNUM"),
    }.get(dialect_for(kind), lambda up: False)
    for token in tokenize(sql):
        if token.kind == "word" and tester(token.upper):
            return True
    return False


def apply_limit(sql: str, n: int, kind: str = "sqlite") -> str:
    """Return ``sql`` with a row cap added when the dialect supports appending
    one and none is already present. Unsupported or ambiguous queries are
    returned unchanged - the connector still slices results client-side."""
    if n <= 0 or has_limit(sql, kind):
        return sql
    dialect = dialect_for(kind)
    stripped = sql.rstrip().rstrip(";").rstrip()
    if not _single_select(tokenize(stripped)):
        return sql
    if dialect == "mssql":
        m = re.match(r"(?is)\bselect\b\s+", stripped)
        if m:
            return stripped[: m.end()] + f"TOP {n} " + stripped[m.end():]
        return sql
    if dialect == "oracle":
        # Oracle 12c+ supports FETCH FIRST.
        return stripped + f" FETCH FIRST {int(n)} ROWS ONLY"
    if dialect in ("mysql", "postgres", "sqlite"):
        return f"{stripped} LIMIT {int(n)}"
    return sql


def _single_select(tokens: list[Token]) -> bool:
    words = [t.upper for t in tokens if t.kind == "word"]
    if not words:
        return False
    first = words[0]
    return first in ("SELECT", "WITH")


def summarize(sql: str, limit: int = 400) -> str:
    one = " ".join(sql.split())
    return one[:limit] + ("…" if len(one) > limit else "")