#!/usr/bin/env python3
"""
Fitness Tracker MCP Server — Remote HTTP wrapper

Imports the existing FastMCP instance and serves it over Streamable HTTP.
Uses the MCP app as the base (so its lifespan initializes the task group),
then prepends our custom routes for protocol discovery, backup, and restore.
"""

import os
import sys
import hmac

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, FileResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# 1. Import the existing MCP server instance
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fitness_mcp as _fm  # noqa: E402
mcp = _fm.mcp


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


# ---------------------------------------------------------------------------
# 3. Auth middleware — only gates /backup and /restore
# ---------------------------------------------------------------------------
PROTECTED_PATHS = {"/backup", "/restore"}


class PathRewriteMiddleware(BaseHTTPMiddleware):
    """Rewrite POST/GET/DELETE on / to /mcp.
    Claude's Custom Connector POSTs to root, but the MCP SDK serves at /mcp.
    HEAD stays at / for our protocol-discovery handler."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/" and request.method in ("POST", "GET", "DELETE"):
            request.scope["path"] = "/mcp"
        return await call_next(request)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if AUTH_TOKEN and request.url.path in PROTECTED_PATHS:
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {AUTH_TOKEN}"
            if not hmac.compare_digest(auth, expected):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="fitness-mcp"'},
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# 4. Custom route handlers
# ---------------------------------------------------------------------------
async def head_root(request: Request) -> Response:
    """Protocol discovery — Claude sends HEAD / to find the MCP version."""
    return Response(
        status_code=200,
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def backup_db(request: Request) -> Response:
    """GET /backup — download the SQLite DB file."""
    db_path = os.environ.get("DB_PATH", _fm.DB_PATH)
    if not os.path.exists(db_path):
        return JSONResponse({"error": "database not found"}, status_code=404)
    return FileResponse(
        db_path, filename="fitness.db", media_type="application/x-sqlite3",
    )


async def restore_db(request: Request) -> Response:
    """POST /restore — upload a SQLite DB to replace the current one."""
    import shutil
    import tempfile

    db_path = os.environ.get("DB_PATH", _fm.DB_PATH)

    form = await request.form()
    upload = form.get("file")
    if upload is None:
        return JSONResponse(
            {"error": "no file provided — use: curl -F 'file=@path/to/fitness.db'"},
            status_code=400,
        )

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(db_path))
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            contents = await upload.read()
            tmp.write(contents)

        if not contents[:16].startswith(b"SQLite format 3"):
            os.unlink(tmp_path)
            return JSONResponse(
                {"error": "uploaded file is not a valid SQLite database"},
                status_code=400,
            )

        shutil.move(tmp_path, db_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return JSONResponse({"error": str(e)}, status_code=500)

    size_kb = len(contents) / 1024
    return JSONResponse({"status": "restored", "size_kb": round(size_kb, 1)})


# ---------------------------------------------------------------------------
# 5. Assemble the app
#
#    KEY: we use the app returned by streamable_http_app() as the base.
#    This preserves its lifespan handler, which initializes the async task
#    group that the MCP session manager needs. If we wrap it inside a
#    separate Starlette app, the sub-app's lifespan never fires and we get:
#      RuntimeError: Task group is not initialized.
#
#    We prepend our routes so they match before the MCP catch-all.
# ---------------------------------------------------------------------------
app = mcp.streamable_http_app()

# Prepend our custom routes (HEAD / for protocol discovery goes first
# so it matches before the MCP catch-all route at /)
for route in reversed([
    Route("/", head_root, methods=["HEAD"]),
    Route("/health", health, methods=["GET"]),
    Route("/backup", backup_db, methods=["GET"]),
    Route("/restore", restore_db, methods=["POST"]),
]):
    app.router.routes.insert(0, route)

# Middleware (Starlette runs last-added first)
if AUTH_TOKEN:
    app.add_middleware(BearerAuthMiddleware)
app.add_middleware(PathRewriteMiddleware)


# ---------------------------------------------------------------------------
# 6. Dev entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"Starting fitness-mcp HTTP server on {HOST}:{PORT}")
    print(f"Auth: {'bearer token' if AUTH_TOKEN else 'authless'}")
    uvicorn.run(app, host=HOST, port=PORT)
