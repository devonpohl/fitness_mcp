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
# 3. Auth middleware
#    Only gates /backup and /restore (admin endpoints).
#    The MCP endpoint (/mcp) and protocol discovery (HEAD /) run authless
#    because Claude's Custom Connector doesn't support bearer tokens —
#    it only does OAuth or no-auth.
# ---------------------------------------------------------------------------
PROTECTED_PATHS = {"/backup", "/restore"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Bearer-token gate for admin endpoints only.
    /mcp, HEAD /, and /health pass through without auth.
    """

    async def dispatch(self, request: Request, call_next):
        if AUTH_TOKEN and request.url.path in PROTECTED_PATHS:
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {AUTH_TOKEN}"
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
# 6. Database restore endpoint
#    POST /restore → replaces the SQLite DB with an uploaded file.
#    Auth-gated like everything else.
#
#    Usage from your laptop:
#      curl -X POST -H "Authorization: Bearer $TOKEN" \
#           -F "file=@~/.fitness_tracker/fitness.db" \
#           https://your-app.up.railway.app/restore
# ---------------------------------------------------------------------------
async def restore_db(request: Request) -> Response:
    import shutil
    import tempfile

    db_path = os.environ.get("DB_PATH", _fm.DB_PATH)

    # Parse multipart form upload
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        return JSONResponse(
            {"error": "no file provided — use: curl -F 'file=@path/to/fitness.db'"},
            status_code=400,
        )

    # Write to a temp file first, then atomically replace.
    # This avoids corrupting the DB if the upload is interrupted.
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".db", dir=os.path.dirname(db_path)
    )
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            contents = await upload.read()
            tmp.write(contents)

        # Basic sanity check: SQLite files start with "SQLite format 3\000"
        if not contents[:16].startswith(b"SQLite format 3"):
            os.unlink(tmp_path)
            return JSONResponse(
                {"error": "uploaded file is not a valid SQLite database"},
                status_code=400,
            )

        # Atomic replace
        shutil.move(tmp_path, db_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return JSONResponse({"error": str(e)}, status_code=500)

    size_kb = len(contents) / 1024
    return JSONResponse({
        "status": "restored",
        "size_kb": round(size_kb, 1),
    })


# ---------------------------------------------------------------------------
# 8. Assemble the ASGI app
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
        Route("/restore", restore_db, methods=["POST"]),
        Mount("/", app=mcp_app),   # delegates /mcp to the SDK handler
    ],
    middleware=middleware,
)


# ---------------------------------------------------------------------------
# 9. Dev entry point  (production uses `uvicorn deploy.server:app`)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"Starting fitness-mcp HTTP server on {HOST}:{PORT}")
    print(f"Auth: {'bearer token' if AUTH_TOKEN else 'authless (no MCP_AUTH_TOKEN set)'}")
    uvicorn.run(app, host=HOST, port=PORT)
