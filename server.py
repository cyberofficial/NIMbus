"""
Claude Code Proxy - Entry Point

Minimal entry point that imports the app from the api module.
Run with: uv run uvicorn server:app --host 0.0.0.0 --port 8082 --timeout-graceful-shutdown 5
"""

import sys

from api.app import app, create_app

__all__ = ["app", "create_app"]


def run_mcp_server():
    """Run the MCP server with stdio transport."""
    from mcp_server import mcp

    print("Starting NIMbus MCP Server (stdio transport)...", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    import uvicorn

    # Check for --mcp / --mcpwebsearch flag before starting proxy server
    if "--mcpwebsearch" in sys.argv or "--mcp" in sys.argv:
        run_mcp_server()
        sys.exit(0)

    from cli.process_registry import kill_all_best_effort
    from config.settings import get_settings

    settings = get_settings()
    try:
        # timeout_graceful_shutdown ensures uvicorn doesn't hang on task cleanup.
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level="debug",
            timeout_graceful_shutdown=5,
        )
    finally:
        # Safety net: cleanup subprocesses if lifespan shutdown doesn't fully run.
        kill_all_best_effort()
