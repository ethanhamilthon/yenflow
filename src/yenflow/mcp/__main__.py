"""Entrypoint for `python -m yenflow.mcp` — runs the MCP stdio server."""

from __future__ import annotations

from gflow_cli.mcp.server import main_stdio

if __name__ == "__main__":
    main_stdio()
