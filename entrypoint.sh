#!/bin/bash
set -e

# ── Persistent database ─────────────────────────────────────────────
# Railway mounts the volume at /data.  We store the SQLite DB there
# so it survives redeploys.  The env var DB_PATH is read by the
# patched fitness_mcp.py (falls back to ~/.fitness_tracker/fitness.db
# when running locally).
export DB_PATH="${DB_PATH:-/data/fitness.db}"

echo "=== fitness-mcp entrypoint ==="
echo "DB_PATH  = $DB_PATH"
echo "PORT     = ${PORT:-8000}"
echo "AUTH     = $([ -n "$MCP_AUTH_TOKEN" ] && echo 'bearer-token' || echo 'authless')"

# ── Start the HTTP server ────────────────────────────────────────────
exec uvicorn deploy.server:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --log-level info
