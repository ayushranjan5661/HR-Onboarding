---
name: deploy-azure
description: Deploy the HR Onboarding app to Azure App Service (free F1 plan) in the HR-Onboarding resource group. Use when asked to deploy, redeploy, publish, or ship this project to Azure, or to push changed .env values up to Azure app settings.
---

# Deploy HR Onboarding to Azure

Two web apps share one **free (F1) Linux App Service plan** in the
`HR-Onboarding` resource group, `southindia`.

| Piece | Azure app | Runtime | URL |
|---|---|---|---|
| Frontend (static) | `HR-Onboarding-Portal` | PHP 8.4 (nginx serves `wwwroot` as-is) | https://hr-onboarding-portal.azurewebsites.net |
| Backend (FastAPI) | `HR-Onboarding-Portal-API` | Python 3.12 | https://hr-onboarding-portal-api.azurewebsites.net |
| Plan | `ASP-HR-Onboarding` | Linux, F1 free | — |
| Database | Supabase Postgres (external, not in Azure) | — | — |

The frontend is static, so it needs no runtime of its own. PHP is used only
because that image serves `/home/site/wwwroot` directly through nginx with no
startup command and no build step. Nothing in the repo is PHP.

## Prerequisites

- `az` CLI logged in to the `LevelShift-AI-COE` subscription
  (`az account show`; `az login` if not).
- A root `.env` with the real values — it is the source of truth for every
  secret. It is gitignored and is **never** included in the deployment zip.
- The Supabase database already seeded (`python init_db.py`). Azure does not
  run this; it talks to the same database your machine does.

## Deploy

```powershell
# everything: settings + both apps
.\.claude\skills\deploy-azure\scripts\deploy.ps1

# one side only
.\.claude\skills\deploy-azure\scripts\deploy.ps1 -Target backend
.\.claude\skills\deploy-azure\scripts\deploy.ps1 -Target frontend
 
# just re-push .env -> Azure app settings (no code deploy)
.\.claude\skills\deploy-azure\scripts\sync-settings.ps1
```

`deploy.ps1` stages a clean copy, zips it and pushes it with
`az webapp deploy --type zip`. `venv/`, `uploads/`, `__pycache__/`, `.git/`
and `.env` are excluded from the backend zip.

The backend zip mirrors the repo layout rather than shipping `backend/`'s
contents, because `app/services/zoho_push.py` imports `integrations/zoho`
from one level above `backend/`:

```
<app root>/
  backend/...          the FastAPI app, incl. tessdata/ (~35 MB)
  integrations/zoho/   imported by app/services/zoho_push.py
  requirements.txt     copy; Oryx only reads one at the deployment root
  startup.sh           installs tesseract, then execs uvicorn
```

## Tesseract OCR

`app/agents/doc_validator.py` shells out to the `tesseract` binary. The
built-in Python image does not ship it and a built-in runtime cannot have a
package baked in, so `startup.sh` installs it with `apt-get` at container
start. Only the engine: the 16 language packs ship in `backend/tessdata` and
`config.py` points `TESSDATA_DIR` at them.

What this costs on F1:

- **~30-60s added to every cold start.** There is no Always On, so the
  container is recycled after ~20 minutes idle and the install runs again.
- **OCR CPU counts against the plan's 60 CPU-minutes/day**, shared with the
  frontend app. A burst of document uploads can exhaust it, and then *both*
  apps return 403 until UTC midnight. Watch this if validation is left in
  `warn` mode (the default — it OCRs every upload).

Moving the plan to B1 removes both problems: the container stays alive, so the
install happens once per deploy, and there is no CPU cap.

If the install fails (mirror unreachable, say) the script logs a warning and
starts the app anyway. `doc_validator._ocr_available()` then returns False and
validation stays off — uploads keep working, they are just not checked. Set
`DOC_VALIDATION_MODE=off` in app settings to disable it deliberately.

## How config reaches the backend

`backend/app/config.py` anchors its `.env` lookup to the **project root**
(`Path(__file__).parents[2] / ".env"`). On Azure that path does not exist, so
pydantic-settings falls back to environment variables — which is exactly what
App Service application settings become. So:

- **Local**: values come from the root `.env`.
- **Azure**: the same keys come from app settings, pushed by `sync-settings.ps1`.

Three keys are deliberately **overridden** on Azure rather than copied:

| Key | Azure value | Why |
|---|---|---|
| `FRONTEND_ORIGINS` | the frontend's Azure URL | CORS must name the deployed origin, not localhost |
| `PORTAL_BASE_URL` | the frontend's Azure URL | candidate invite links are built from it |
| `UPLOAD_DIR` | `/home/uploads` | `/home` is the only persistent disk; anything under `wwwroot` is replaced on every deploy |

`JWT_SECRET_KEY` must be set or the app refuses to boot by design
([config.py](../../../backend/app/config.py) raises on the placeholder value).

## How the frontend finds the backend

`frontend/js/config.js` picks `API_BASE` from `location.hostname`, so one file
serves every environment and nothing is rewritten at deploy time:

- `localhost` / `127.0.0.1` -> `http://127.0.0.1:8000`
- `hr-onboarding-portal.azurewebsites.net` -> the Azure API
- anything else -> the old Render backend

If you rename an app, update that mapping and `sync-settings.ps1`'s
`$FrontendUrl` together, or logins fail with a bare "Failed to fetch".

## Free-tier limits worth knowing

- **60 CPU-minutes/day** across both apps, shared. Exceeding it returns 403
  until the quota resets at UTC midnight.
- **15 worker-process stops** (`WPStopRequests`) per quota window, also shared
  across both apps. A container that crash-loops burns through this in minutes,
  and the whole *plan* then goes `QuotaExceeded` -- both apps serve
  "Error 403 - This web app is stopped" even though only one was misbehaving.
  This is the limit you will actually hit first, not CPU. Check it with:
  `az rest --method get --url ".../serverfarms/ASP-HR-Onboarding/usages?api-version=2023-01-01"`
  The single most common cause is setting the startup command on an app that
  has no code deployed yet, which is why `deploy.ps1` pushes the zip first.
- **No Always On** — the apps cold-start after ~20 minutes idle. The first
  request after a sleep takes a few seconds. This is the free tier, not a bug.
- **No custom domain and no SSL binding** on F1. To put this on a
  `levelshift.com` hostname the plan must move to B1 or higher
  (`az appservice plan update -g HR-Onboarding -n ASP-HR-Onboarding --sku B1`).
- 1 GB storage, shared across both apps and the uploads directory.

## Verify a deploy

```powershell
# backend up and talking to Supabase (401 = reached the DB, rejected a fake user)
curl.exe -s -o NUL -w "%{http_code}`n" -X POST `
  https://hr-onboarding-portal-api.azurewebsites.net/auth/hr/login `
  -H "Content-Type: application/json" -d "{\"email\":\"probe@x.com\",\"password\":\"probe\"}"

# frontend serving
curl.exe -s -o NUL -w "%{http_code}`n" https://hr-onboarding-portal.azurewebsites.net/index.html

# live log tail
az webapp log tail -g HR-Onboarding -n HR-Onboarding-Portal-API
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Backend 500 on every request | App settings missing — run `sync-settings.ps1`. Check `az webapp log tail`. |
| "Application Error" / container won't start | Startup command wrong, or a dependency failed to build. `SCM_DO_BUILD_DURING_DEPLOYMENT=true` must be set *before* the zip push, or Oryx skips `pip install`. Fix it quickly -- a crash loop burns the plan's WPStopRequests quota and takes the other app down with it. |
| Both apps 403 "This web app is stopped" while `state` says Running | Plan-level `QuotaExceeded`. Check `usageState` with `az webapp show`, then the plan `usages` endpoint to see which counter blew. Stop the offending app (`az webapp stop`) so it cannot keep restarting, and wait for the window to reset. |
| Login fails with "Failed to fetch" | `API_BASE` in `config.js` and `FRONTEND_ORIGINS` disagree. Both must name the exact deployed origin, scheme included. |
| 403 on every request, suddenly | F1 daily CPU quota exhausted. Wait for UTC midnight or move the plan to B1. |
| Uploaded files vanish after a deploy | `UPLOAD_DIR` is pointing inside `wwwroot`. It must be `/home/uploads`. |
| Container exits immediately, log names a binary that exists | `startup.sh` has CRLF line endings, so the shebang reads `#!/usr/bin/env bash`. `deploy.ps1` strips CR from it when staging; do not bypass that. |
| Documents never get validated, no error anywhere | Tesseract missing. By design the app starts anyway. Check the startup log for `[startup] WARNING`, and `DOC_VALIDATION_MODE` in app settings. |
