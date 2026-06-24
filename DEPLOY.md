# Deploying the Bridge

The Bridge must run where it can reach its dependencies. **Deploy to a US region** so the
FMCSA QCMobile API (geo-restricted to US IPs) works with `FMCSA_MODE=live`.

## Environment variables (set these on the host, never commit them)

| Var | Value |
|---|---|
| `TMS_HOST` / `TMS_PORT` / `TMS_TOKEN` | from the TMS provisioning email |
| `FMCSA_API_KEY` | your QCMobile webKey |
| `FMCSA_MODE` | `live` (US deploy) |
| `BRIDGE_API_KEY` | a strong secret — the platform Webhooks send it as `X-API-Key` |

Generate the Bridge key: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

---

## Option A — Railway (recommended; the TMS already lives on Railway)

Railway builds the `Dockerfile`, gives a public HTTPS URL, and `railway.json` pins the
health check. **One-time setup, then a one-command deploy.**

```bash
# 1. install + login (interactive — run in your terminal)
npm i -g @railway/cli
railway login

# 2. from bridge/, create/link a project
railway init            # or: railway link  (to an existing project)

# 3. set the environment variables (or paste them in the Railway dashboard → Variables)
railway variables --set TMS_HOST=... --set TMS_PORT=... --set TMS_TOKEN=... \
                  --set FMCSA_API_KEY=... --set FMCSA_MODE=live \
                  --set BRIDGE_API_KEY=...

# 4. deploy
railway up
```

Then in the Railway dashboard:
- **Settings → Region** → pick a **US** region (e.g. `us-east4`/`us-west1`) so FMCSA `live` works.
- **Settings → Networking → Generate Domain** → this is your `BRIDGE_URL` for [`WEBHOOKS.md`](./WEBHOOKS.md).
- Health check (`/health`) and restart policy come from `railway.json`.

> Redeploys are just `railway up` (or auto-deploy on git push if you connect the GitHub repo).

---

## Option B — Any Docker host (single command)

On a US-region VM (EC2/GCE/DO in a US region), with a `.env` containing the variables above
(and `FMCSA_MODE=live`):

```bash
docker compose up --build -d
```

The service listens on `:8000` (compose maps it). Put it behind your reverse proxy / TLS, and
that public URL becomes `BRIDGE_URL`. The container respects `$PORT` if your platform injects one.

---

## Verify the deploy

```bash
curl -s https://<your-bridge-url>/health            # {"status":"ok","tms":"up",...}

# FMCSA now live from the US egress:
curl -s -X POST https://<your-bridge-url>/verify-carrier \
  -H "X-API-Key: $BRIDGE_API_KEY" -H "Content-Type: application/json" \
  -d '{"mc_number":"872144"}'
```

If `/health` shows `"tms":"up"` and verify-carrier returns real FMCSA data, you're ready to wire
the platform Webhooks ([`WEBHOOKS.md`](./WEBHOOKS.md)).
