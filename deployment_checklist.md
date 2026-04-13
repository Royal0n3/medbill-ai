# Railway Deployment Checklist — MedBill AI

## Prerequisites
- Railway CLI installed: `npm install -g @railway/cli`
- Railway account at https://railway.app
- Git repository initialised and all files committed

---

## Step 1 — Login and initialise

```bash
railway login
cd /path/to/medbill-ai
railway init
```

Select **"Empty project"** when prompted, then name it (e.g. `medbill-ai`).

---

## Step 2 — Link the project (if already created in the dashboard)

```bash
railway link
```

Select your project and the `production` environment.

---

## Step 3 — Set environment variables

In the **Railway dashboard → your project → Variables**, add:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` |
| `FLASK_SECRET_KEY` | a long random string (e.g. `openssl rand -hex 32`) |
| `BREVO_API_KEY` | your Brevo key (optional — for email features) |

Alternatively, set them via CLI:

```bash
railway variables set ANTHROPIC_API_KEY=sk-ant-api03-...
railway variables set FLASK_SECRET_KEY=$(openssl rand -hex 32)
```

> Never commit `.env` to git — it is covered by `.gitignore`.

---

## Step 4 — Deploy

```bash
git add .
git commit -m "Add Railway deployment config"
railway up
```

Railway will:
1. Detect Python via Nixpacks
2. Run `pip install -r requirements.txt`
3. Start gunicorn via the Procfile / railway.toml start command
4. Poll `/health` to confirm the service is live

---

## Step 5 — Get your public URL

```bash
railway domain
```

Or in the dashboard: **Settings → Networking → Generate Domain**.

Railway assigns a `*.up.railway.app` subdomain automatically.

---

## Step 6 — Link a custom domain

1. In the dashboard go to **Settings → Networking → Custom Domain**.
2. Enter your domain (e.g. `app.medbill.ai`).
3. Copy the `CNAME` target shown (e.g. `abc123.up.railway.app`).
4. In your DNS provider, add:
   ```
   Type: CNAME
   Name: app   (or @ for apex)
   Value: abc123.up.railway.app
   TTL: 3600
   ```
5. Railway provisions an SSL certificate automatically within a few minutes.

---

## Step 7 — Verify /upload is live

```bash
curl -s https://your-app.up.railway.app/health
# Expected: {"status":"ok"}

curl -s -X POST https://your-app.up.railway.app/upload \
  -F "file=@sample_bill.pdf" | python -m json.tool
# Expected: {"bill_id":"...","filename":"...","extraction":{...}}
```

---

## Ongoing deploys

Every `railway up` (or a push to your linked GitHub branch) triggers a new deploy. Monitor logs with:

```bash
railway logs
```
