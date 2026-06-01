"""MCP tools that expose the bridge-managed CodeGraph indexes to Codex."""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .knowledge.codegraph_client import CodeGraphClient


_MAX_ROOTS = 8
_MAX_HITS = 20


def _configured_roots() -> list[Path]:
    raw = os.environ.get("LARK_AGENT_BRIDGE_CODEGRAPH_ROOTS", "")
    roots: list[Path] = []
    for item in raw.splitlines():
        item = item.strip()
        if not item:
            continue
        root = Path(item).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved.exists() and resolved not in roots:
            roots.append(resolved)
        if len(roots) >= _MAX_ROOTS:
            break
    return roots


def _client() -> CodeGraphClient:
    timeout_raw = os.environ.get("LARK_AGENT_BRIDGE_CODEGRAPH_TIMEOUT_SECONDS", "10")
    try:
        timeout = max(1.0, float(timeout_raw))
    except ValueError:
        timeout = 10.0
    command = (os.environ.get("LARK_AGENT_BRIDGE_CODEGRAPH_COMMAND") or "codegraph").strip() or "codegraph"
    return CodeGraphClient(command=command, timeout=timeout)


def _indexed_roots(client: CodeGraphClient) -> list[Path]:
    return [root for root in _configured_roots() if client.is_indexed(root)]


def _header(root: Path) -> str:
    return f"# {root.name}: {root}"


mcp = FastMCP(
    "bridge_codegraph",
    instructions=(
        "Use these tools to query the bridge-managed CodeGraph indexes. "
        "Treat results as candidates and read the concrete source lines before citing evidence."
    ),
)


@mcp.tool()
def codegraph_status() -> str:
    """Return CodeGraph index availability for the configured source roots."""
    client = _client()
    roots = _configured_roots()
    if not roots:
        return "No CodeGraph source roots are configured."
    lines: list[str] = []
    for root in roots:
        indexed = client.is_indexed(root)
        lines.append(f"- {root}: {'indexed' if indexed else 'not indexed or unavailable'}")
    return "\n".join(lines)


@mcp.tool()
def search_codegraph(query: str, kind: str = "") -> str:
    """Search CodeGraph symbols by class, method, function, field, or signal name."""
    query = (query or "").strip()
    if not query:
        return "query is required"
    client = _client()
    roots = _indexed_roots(client)
    if not roots:
        return "No indexed CodeGraph roots are available."
    sections: list[str] = []
    for root in roots:
        hits = client.search_symbol(query, root, limit=10, kind=(kind or "").strip() or None)
        if not hits:
            continue
        lines = [_header(root)]
        for hit in hits[:_MAX_HITS]:
            name = hit.qualified_name or hit.name
            signature = f" | {hit.signature}" if hit.signature else ""
            lines.append(f"- [{hit.kind}] {name} @ {hit.path}:{hit.line}{signature}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else f"No symbols found for: {query}"


@mcp.tool()
def get_callers(symbol: str) -> str:
    """Return direct callers for a symbol across the configured CodeGraph roots."""
    symbol = (symbol or "").strip()
    if not symbol:
        return "symbol is required"
    client = _client()
    roots = _indexed_roots(client)
    if not roots:
        return "No indexed CodeGraph roots are available."
    sections: list[str] = []
    for root in roots:
        callers = client.get_callers(symbol, root, limit=20)
        if not callers:
            continue
        lines = [_header(root)]
        for caller in callers[:_MAX_HITS]:
            lines.append(f"- [{caller.kind}] {caller.name} @ {caller.path}:{caller.line}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else f"No callers found for: {symbol}"


@mcp.tool()
def get_code_context(task: str) -> str:
    """Build CodeGraph context for a task across the configured source roots."""
    task = (task or "").strip()
    if not task:
        return "task is required"
    client = _client()
    roots = _indexed_roots(client)
    if not roots:
        return "No indexed CodeGraph roots are available."
    sections: list[str] = []
    for root in roots:
        context = client.get_context(task, root, max_nodes=30)
        if not context.entry_points and not context.summary:
            continue
        lines = [_header(root)]
        if context.summary:
            lines.append(f"summary: {context.summary[:500]}")
        if context.entry_points:
            lines.append("entry points:")
            for entry in context.entry_points[:12]:
                name = entry.get("qualifiedName") or entry.get("name") or ""
                kind = entry.get("kind") or ""
                path = entry.get("filePath") or ""
                line = entry.get("startLine") or ""
                lines.append(f"- [{kind}] {name} @ {path}:{line}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else f"No code context found for: {task}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
