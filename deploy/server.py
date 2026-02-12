#!/usr/bin/env python3
"""
Fitness Tracker MCP Server — Remote HTTP wrapper

This file replaces the `if __name__ == "__main__": mcp.run()` entry point.
It imports the existing FastMCP instance and runs it with Streamable HTTP
transport, adding the protocol-discovery HEAD endpoint that Claude's
Custom Connector requires.

The actual tool definitions, DB logic, and everything else stays in
fitness_mcp.py — this file only handles transport, auth, and backup.
"""

import os
import sys
import hmac

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, FileResponse
from starlette.routing import Route, Mount

# ---------------------------------------------------------------------------
# 1. Import the existing MCP server instance
#    We add the parent dir to sys.path so `import fitness_mcp` works when
#    this file lives in deploy/
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fitness_mcp as _fm  # noqa: E402  — triggers init_db() & tool registration
mcp = _fm.mcp


# ---------------------------------------------------------------------------
# 2. Configuration from environment
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

# Optional bearer-token auth.  Set MCP_AUTH_TOKEN in your deploy environment
# to a long random string.  If unset, the server runs authless (fine for
# Claude Custom Connectors that don't need OAuth — you just paste the URL).
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


# ---------------------------------------------------------------------------
# 3. Auth middleware  (skipped if AUTH_TOKEN is empty → authless mode)
# ---------------------------------------------------------------------------
class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Lightweight bearer-token gate.  If MCP_AUTH_TOKEN is set, every request
    except the protocol-discovery HEAD must carry
        Authorization: Bearer <token>
    Claude's Custom Connector "Advanced settings" lets you supply a static
    token, so this is the simplest auth path that still locks the door.
    """

    async def dispatch(self, request: Request, call_next):
        # Always let HEAD through — Claude uses it for protocol discovery
        if request.method == "HEAD":
            return await call_next(request)

        # Health-check endpoint is unauthenticated
        if request.url.path == "/health":
            return await call_next(request)

        if AUTH_TOKEN:
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {AUTH_TOKEN}"
            # constant-time compare to avoid timing attacks
            if not hmac.compare_digest(auth, expected):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="fitness-mcp"',
                    },
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# 4. Protocol-discovery endpoint
#    Claude sends HEAD / to discover the MCP protocol version.
#    This MUST return the header `MCP-Protocol-Version: 2025-06-18`.
#    Without it, the Custom Connector shows "Disconnected" with no feedback.
# ---------------------------------------------------------------------------
async def head_root(request: Request) -> Response:
    return Response(
        status_code=200,
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )


async def health(request: Request) -> Response:
    """Unauthenticated health check for Railway uptime monitors."""
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# 5. Database backup endpoint
#    GET /backup → streams the raw SQLite file back to you.
#    Auth-gated like everything else (if MCP_AUTH_TOKEN is set).
#
#    Usage from your laptop:
#      curl -H "Authorization: Bearer $TOKEN" \
#           https://your-app.up.railway.app/backup \
#           -o ~/.fitness_tracker/fitness.db
# ---------------------------------------------------------------------------
async def backup_db(request: Request) -> Response:
    db_path = os.environ.get("DB_PATH", _fm.DB_PATH)
    if not os.path.exists(db_path):
        return JSONResponse({"error": "database not found"}, status_code=404)
    return FileResponse(
        db_path,
        filename="fitness.db",
        media_type="application/x-sqlite3",
    )


# ---------------------------------------------------------------------------
# 6. Assemble the ASGI app
#    FastMCP.streamable_http_app() returns a Starlette/ASGI sub-application
#    that handles POST /mcp (the actual MCP RPC endpoint).
#    We mount it at "/" so the full path is just "/mcp".
#    Claude expects to POST to <your-url>/mcp after discovering the protocol
#    version via HEAD <your-url>/.
# ---------------------------------------------------------------------------

# Get the MCP ASGI sub-app (handles /mcp endpoint)
mcp_app = mcp.streamable_http_app()

middleware = []
if AUTH_TOKEN:
    middleware.append(Middleware(BearerAuthMiddleware))

app = Starlette(
    routes=[
        Route("/", head_root, methods=["HEAD"]),
        Route("/health", health, methods=["GET"]),
        Route("/backup", backup_db, methods=["GET"]),
        Mount("/", app=mcp_app),   # delegates /mcp to the SDK handler
    ],
    middleware=middleware,
)


# ---------------------------------------------------------------------------
# 7. Dev entry point  (production uses `uvicorn deploy.server:app`)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"Starting fitness-mcp HTTP server on {HOST}:{PORT}")
    print(f"Auth: {'bearer token' if AUTH_TOKEN else 'authless (no MCP_AUTH_TOKEN set)'}")
    uvicorn.run(app, host=HOST, port=PORT)
