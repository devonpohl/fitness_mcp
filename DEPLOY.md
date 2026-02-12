# Deploying fitness-mcp on Railway

## Architecture

```
┌──────────────────────┐         ┌──────────────────────────┐
│  Claude.ai / Mobile  │  HTTPS  │  Railway service          │
│  Custom Connector    │────────▶│  uvicorn → Starlette      │
│                      │         │  ├─ HEAD /    (discovery)  │
│                      │         │  ├─ POST /mcp (MCP tools)  │
│                      │         │  ├─ GET /health            │
│                      │         │  └─ GET /backup (DB dump)  │
│  Your laptop         │         │                            │
│  curl /backup        │────────▶│  SQLite on /data volume    │
└──────────────────────┘         └──────────────────────────┘
```

**What changed from the local stdio server:**

1. `DB_PATH` in fitness_mcp.py reads from env var (defaults to original path locally)
2. New `deploy/server.py` wraps the existing `mcp` instance with HTTP transport
3. `GET /backup` endpoint lets you download the DB file to your laptop
4. Dockerfile + entrypoint.sh for containerized deploy
5. railway.toml + Railway volume for persistent SQLite

**What didn't change:** Every tool definition, Pydantic model, DB schema, and
query in `fitness_mcp.py` is untouched (aside from the 2-line DB_PATH patch).
Your local stdio setup still works via `python fitness_mcp.py`.

---

## Step 1: Push to GitHub

Railway deploys from a GitHub repo. If you don't already have one:

```bash
cd /path/to/fitness_mcp
git init
git add fitness_mcp.py deploy/ Dockerfile entrypoint.sh \
        railway.toml requirements-remote.txt requirements.txt \
        .dockerignore
git commit -m "Add remote HTTP transport for Railway deploy"
git remote add origin git@github.com:YOUR_USER/fitness-mcp.git
git push -u origin main
```

## Step 2: Create Railway service

1. Go to [railway.app/new](https://railway.app/new)
2. Click **"Deploy from GitHub Repo"**
3. Select your `fitness-mcp` repo
4. Railway auto-detects the Dockerfile via `railway.toml`

## Step 3: Attach a volume

1. In your Railway project, open the service
2. Hit **Cmd+K** (command palette) → **"Add Volume"**
3. Set the mount path to `/data`
4. This is where `/data/fitness.db` lives — it persists across redeploys

## Step 4: Set environment variables

In the service's **Variables** tab:

```
RAILWAY_RUN_UID=0
```

This is needed because Railway mounts volumes as root. Without it you get
`SQLITE_READONLY` errors.

For bearer-token auth (recommended):

```bash
# Generate a token locally
openssl rand -hex 32
```

Then add to Railway variables:

```
MCP_AUTH_TOKEN=<paste your token here>
```

## Step 5: Deploy

Railway auto-deploys on push to main. Or click **Deploy** in the dashboard.

Verify:

```bash
# Should return MCP-Protocol-Version header
curl -I https://YOUR-APP.up.railway.app/

# Should return {"status": "ok"}
curl https://YOUR-APP.up.railway.app/health
```

## Step 6: Migrate your existing database

Push your local DB up to Railway via the `/restore` endpoint:

```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
     -F "file=@$HOME/.fitness_tracker/fitness.db" \
     https://YOUR-APP.up.railway.app/restore
```

If it's authless (no `MCP_AUTH_TOKEN`), drop the `-H` flag.

You should see `{"status": "restored", "size_kb": ...}` on success.
The endpoint validates that the uploaded file is actually a SQLite DB
before replacing anything.

## Step 7: Add as Claude Custom Connector

1. Go to **claude.ai** → **Settings** → **Connectors**
2. Click **"Add custom connector"**
3. Enter URL: `https://YOUR-APP.up.railway.app/`
4. If you set `MCP_AUTH_TOKEN`:
   - Click **"Advanced settings"**
   - Enter your token
5. Click **"Add"**

Your 28 fitness tools now work on web + mobile.

---

## Backing up the database

The `/backup` endpoint streams the raw SQLite file. It's auth-gated
(if you set `MCP_AUTH_TOKEN`).

**One-off backup:**

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
     https://YOUR-APP.up.railway.app/backup \
     -o ~/.fitness_tracker/fitness.db
```

**Weekly cron (add to your crontab):**

```bash
# Every Sunday at midnight, back up fitness DB
0 0 * * 0 curl -sS -H "Authorization: Bearer YOUR_TOKEN" \
    https://YOUR-APP.up.railway.app/backup \
    -o ~/backups/fitness-$(date +\%Y\%m\%d).db
```

**Alias (add to .bashrc/.zshrc):**

```bash
alias fitness-backup='curl -H "Authorization: Bearer YOUR_TOKEN" \
    https://YOUR-APP.up.railway.app/backup \
    -o ~/.fitness_tracker/fitness.db && echo "backed up"'
```

---

## Local testing

```bash
pip install -r requirements-remote.txt

# Run locally (uses ~/.fitness_tracker/fitness.db)
python deploy/server.py

# Test endpoints
curl -I http://localhost:8000/
curl http://localhost:8000/health
curl http://localhost:8000/backup -o /tmp/test-backup.db

# Test with MCP Inspector
npx @modelcontextprotocol/inspector --url http://localhost:8000/mcp
```

---

## Troubleshooting

**"Disconnected" in Claude with no error:**
- `HEAD /` must return `MCP-Protocol-Version: 2025-06-18` header
- The MCP endpoint must be at `/mcp` (the SDK default)
- If using auth, make sure the token matches exactly

**SQLITE_READONLY:**
- Add `RAILWAY_RUN_UID=0` to your service variables
- Redeploy after adding it

**Database resets on redeploy:**
- Make sure a volume is attached with mount path `/data`
- Check Railway dashboard → service → Volumes

**Google Calendar tools not working remotely:**
- `InstalledAppFlow.run_local_server()` needs a browser (no browser in a container)
- Either pre-auth locally and upload `token.json`, or keep calendar as local-only
