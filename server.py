"""
MCP server — entry point.

This is the main file you run.  It creates the FastMCP instance,
registers tools from sub-modules (Rhea + Wikidata), and sets up the
ASGI application with health-check routing.

Architecture:
  server.py             ← you are here
  tools/
    __init__.py          — package docstring
    shared.py            — HTTP client, SPARQL execution, caching, linting
    rhea.py              — Rhea biochemical reaction tools
    wikidata.py          — Wikidata grounding + SPARQL tools

Run:
  python server.py           (default port 8080)
  PORT=9000 python server.py (custom port)
"""

import os
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 1. Create the MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("graph-bio")
mcp.settings.streamable_http_path = "/"

# ---------------------------------------------------------------------------
# 2. Register tools from each sub-module
# ---------------------------------------------------------------------------

from tools import rhea, wikidata  # noqa: E402

rhea.register(mcp)
wikidata.register(mcp)

# ---------------------------------------------------------------------------
# 3. Build the ASGI application
# ---------------------------------------------------------------------------

sse_app = mcp.streamable_http_app()


async def _plain_200(scope, receive, send, body: str = "MCP server OK."):
    """Send a simple 200 text/plain response (health checks, root page)."""
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"access-control-allow-origin", b"*"),
    ]
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": headers,
    })
    await send({
        "type": "http.response.body",
        "body": body.encode("utf-8"),
        "more_body": False,
    })


class RootOrSSE:
    """
    ASGI router:
      /healthz                      → plain "ok"
      Accept: text/event-stream     → forward to MCP SSE handler
      everything else               → plain 200 landing page
    """

    def __init__(self, sse):
        self.sse = sse

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.sse(scope, receive, send)

        path = scope.get("path", "/") or "/"
        hdrs = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope.get("headers", [])
        }
        accept = hdrs.get("accept", "")

        if path == "/healthz":
            return await _plain_200(scope, receive, send, "ok")

        # MCP clients send Accept: text/event-stream
        if "text/event-stream" in accept:
            return await self.sse(scope, receive, send)

        return await _plain_200(scope, receive, send)


app = RootOrSSE(sse_app)

# ---------------------------------------------------------------------------
# 4. Dev server (python server.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=True,
    )
