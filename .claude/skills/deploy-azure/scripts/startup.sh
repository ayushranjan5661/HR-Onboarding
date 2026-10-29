#!/usr/bin/env bash
# Azure App Service Linux startup for the HR Onboarding API.
#
# Why this exists: app/agents/doc_validator.py shells out to the `tesseract`
# binary (deliberately not pytesseract -- see its docstring). The built-in
# Python image does not ship Tesseract, and there is no way to bake a package
# into a built-in runtime, so it is installed here at container start.
#
# On the F1 plan there is no Always On, so the container is torn down after
# ~20 minutes idle and this install runs again on the next cold start,
# costing ~30-60s and some of the plan's 60 CPU-minutes/day. That is the
# accepted trade-off for staying on the free tier; moving the plan to B1
# would keep the container alive and make this a once-per-deploy cost.
#
# Only the binary is installed. The language packs ship in the repo at
# backend/tessdata (16 scripts, ~35 MB) and config.py points TESSDATA_DIR
# at them, so no language packages are pulled from apt.
set -u

if command -v tesseract >/dev/null 2>&1; then
    echo "[startup] tesseract already present: $(command -v tesseract)"
else
    echo "[startup] installing tesseract-ocr ..."
    # --no-install-recommends keeps this to the engine and its direct
    # dependencies; the recommended set drags in all of apt's language data,
    # which we already ship and which would make every cold start far slower.
    if apt-get update -qq >/dev/null 2>&1 &&
       apt-get install -y -qq --no-install-recommends tesseract-ocr >/dev/null 2>&1; then
        echo "[startup] installed: $(tesseract --version 2>&1 | head -1)"
    else
        # Never fail the app over this. doc_validator._ocr_available() returns
        # False when the binary is missing and validation silently stays off,
        # which is a much better outcome than the whole API refusing to boot
        # because a package mirror was briefly unreachable.
        echo "[startup] WARNING: tesseract install failed - document validation will stay off"
    fi
fi

# exec so uvicorn becomes PID 1 and receives the platform's stop signals
# directly; without it SIGTERM goes to this script and the workers are killed
# rather than shut down.
exec python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
