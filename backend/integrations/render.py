"""Render query results / discovery into compact text for the model and log."""

from __future__ import annotations

_MAX_CELL = 120


def render_rows(columns: list[str], rows: list[list], max_chars: int = 6000) -> str:
    """Textualise rows like an aligned table; degenerate cases become a list."""
    if not rows:
        return "(no rows)"
    if not columns:
        return "(ok)"
    colw: list[int] = []
    for index, name in enumerate(columns):
        width = len(str(name))
        for row in rows:
            width = max(width, _cell_width(row[index] if index < len(row) else ""))
        colw.append(min(width, _MAX_CELL))
    header = " | ".join(str(c)[: _MAX_CELL].ljust(colw[i]) for i, c in enumerate(columns))
    lines = [header, "-+-".join("-" * w for w in colw)]
    for row in rows:
        cells = []
        for i, name in enumerate(columns):
            raw = row[i] if i < len(row) else ""
            text = str(raw)
            if len(text) > _MAX_CELL:
                text = text[:_MAX_CELL] + "…"
            cells.append(text.ljust(colw[i]))
        lines.append(" | ".join(cells))
    block = "\n".join(lines)
    return block[:max_chars] + ("\n…(truncated)" if len(block) > max_chars else "")


def _cell_width(value) -> int:
    try:
        return max(1, min(len(str(value)), _MAX_CELL))
    except Exception:
        return 1


def discovery_summary(discovery: dict, max_tables: int = 40) -> str:
    kind = discovery.get("kind", "?")
    tables = discovery.get("tables") or []
    out = [f"{kind} schema ({len(tables)} tables/views):"]
    for table in tables[:max_tables]:
        name = table.get("name")
        cols = ", ".join(
            f"{c.get('name')}:{c.get('type')}" for c in (table.get("columns") or [])
        )
        out.append(f"- {name} ({table.get('kind', 'table')}) columns: {cols}")
    rels = discovery.get("relationships") or []
    if rels:
        out.append("relationships:")
        for r in rels[:30]:
            out.append(
                f"- {r.get('table')}.{r.get('column')} -> "
                f"{r.get('references_table')}.{r.get('references_column')}"
            )
    return "\n".join(out)


def tools_summary(tools: list[dict]) -> str:
    if not tools:
        return "(no tools exposed)"
    lines = []
    for tool in tools:
        name = tool.get("name") or tool.get("title") or tool.get("operationId") or "?"
        desc = (tool.get("description") or tool.get("summary") or "").strip()
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines)