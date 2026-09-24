"""
Keeps ZOHO_ACCESS_TOKEN in .env fresh.

    python integrations/zoho/token_refresher.py          # loop, refresh every 50 min
    python integrations/zoho/token_refresher.py --once   # refresh once and exit

Reads ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN /
ZOHO_ACCOUNTS_BASE_URL from .env and rewrites the ZOHO_ACCESS_TOKEN line in
place. Standalone on purpose - no backend imports, so it can run as a service.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(ROOT, ".env")
INTERVAL = 50 * 60


def read_env(path):
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def write_token(path, token):
    """Rewrite the ZOHO_ACCESS_TOKEN line, appending it if absent."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    newline = "ZOHO_ACCESS_TOKEN=%s\n" % token
    for i, line in enumerate(lines):
        if line.strip().startswith("ZOHO_ACCESS_TOKEN="):
            lines[i] = newline
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(newline)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp, path)


def refresh(env):
    missing = [n for n in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
               if not env.get(n)]
    if missing:
        raise RuntimeError("set %s in .env" % ", ".join(missing))

    base = env.get("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.in").rstrip("/")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": env["ZOHO_CLIENT_ID"],
        "client_secret": env["ZOHO_CLIENT_SECRET"],
        "refresh_token": env["ZOHO_REFRESH_TOKEN"],
    }).encode()
    req = urllib.request.Request(base + "/oauth/v2/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")

    # Zoho reports auth failures as HTTP 200 with an "error" key.
    if "access_token" not in payload:
        raise RuntimeError("token refresh failed: %s" % payload.get("error", payload))
    return payload["access_token"], int(payload.get("expires_in", 3600))


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="refresh once and exit")
    ap.add_argument("--interval", type=int, default=INTERVAL, help="seconds between refreshes")
    args = ap.parse_args()

    if not os.path.exists(ENV_PATH):
        sys.exit(".env not found at %s" % ENV_PATH)

    while True:
        try:
            token, expires_in = refresh(read_env(ENV_PATH))
            write_token(ENV_PATH, token)
            log("token refreshed, expires_in=%ss" % expires_in)
        except (RuntimeError, OSError, urllib.error.URLError, ValueError) as exc:
            log("ERROR: %s" % exc)
            if args.once:
                sys.exit(1)
            time.sleep(60)   # transient failure: retry soon, do not wait 50 min
            continue
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
