"""
Zoho People CLI - replaces the Postman collection that used to live here.

One-time, if you do not have a refresh token yet:
    python integrations/zoho/zoho_client.py --exchange-code 1000.abc...

Discovery (read-only):
    python integrations/zoho/zoho_client.py --list-forms
    python integrations/zoho/zoho_client.py --list-fields --form Candidate_Test

Build a payload without touching Zoho (the DEFAULT for a candidate):
    python integrations/zoho/zoho_client.py --candidate-id 7

Write it (nothing is ever sent without --push):
    python integrations/zoho/zoho_client.py --candidate-id 7 --form Candidate_Test --push --draft
    python integrations/zoho/zoho_client.py --candidate-id 7 --form Candidate_Test --push --record-id 1234
    python integrations/zoho/zoho_client.py --verify --form Candidate_Test

Attach the candidate's uploaded documents (photo, Aadhaar, PAN, marksheets...)
to file-upload fields on the Zoho form. Fill in the "files" map in
field_map.json first (--list-fields shows the Zoho API names):
    python integrations/zoho/zoho_client.py --candidate-id 7 --with-files            # dry run
    python integrations/zoho/zoho_client.py --candidate-id 7 --with-files \
        --form Candidate_Test --push --draft

Credentials come from the project .env via app.config.settings - ZOHO_CLIENT_ID,
ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN. Setting ZOHO_ACCESS_TOKEN instead is a
stopgap: it is used as-is and cannot be refreshed, so it 401s after ~1 hour.

The refresh token needs
ZOHOPEOPLE.forms.CREATE (plus .UPDATE for --record-id); forms.READ alone covers
discovery and --verify only. Scopes cannot be added to an existing refresh
token - re-authorize to get a new one.

There is no sandbox here: insertRecord writes a real record into whatever org
the refresh token belongs to. The guards that replace one are:
  * --push required to write, plus an interactive confirmation unless --yes;
  * --form required for every write, never defaulted from .env;
  * a getRecords lookup on the email field before every insert, aborting on a
    hit (Zoho's insertRecord has NO dedupe - running it twice creates two
    records) and also aborting if that lookup itself fails;
  * one candidate per invocation, no batch mode;
  * every request and response appended to out/zoho_push.log (gitignored - it
    contains PII).
Still point --form at a duplicate form (e.g. Candidate_Test) until the payload
round-trips, and use --draft so the first record lands deletable.
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

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
TOKEN_CACHE = os.path.join(OUT_DIR, ".token.json")
LOG_PATH = os.path.join(OUT_DIR, "zoho_push.log")

# export_zoho_payload puts backend/ on sys.path and owns the payload building.
sys.path.insert(0, HERE)
from export_zoho_payload import build_full_payload, collect_values  # noqa: E402

from app.config import settings                                          # noqa: E402
from app.database import SessionLocal                                    # noqa: E402
from app.models import Candidate                                         # noqa: E402

# Zoho names the email field Email_ID on the stock Candidate form; a customised
# form may differ, so the dedupe search field is overridable with --email-field.
DEFAULT_EMAIL_FIELD = "Email_ID"


class ZohoError(RuntimeError):
    pass


class ZohoUnreachable(ZohoError):
    """The request never reached Zoho — DNS/TCP/TLS failed locally, so Zoho
    neither saw nor rejected anything. Callers surface this differently from a
    ZohoError, which always carries a real reply from Zoho."""


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def log(event, **fields):
    os.makedirs(OUT_DIR, exist_ok=True)
    line = json.dumps(
        dict(ts=datetime.now().isoformat(timespec="seconds"), event=event, **fields),
        ensure_ascii=False, default=str)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# A connection-level failure (DNS/TCP, not an HTTP response) happens before
# any bytes of the request are sent, so retrying is always safe — never a
# risk of Zoho having partially received a write. Observed on one org's
# network: the exact same request, back to back seconds apart, failed then
# succeeded — a flaky corporate DNS resolver, not a code or auth problem.
# Attempts back off 1s, 2s, 4s, 8s (~15s of cover) instead of three tries in
# three seconds: the observed outages last tens of seconds, so the old window
# was almost guaranteed to fall inside one. Still short enough to sit behind
# the HR portal's button without the request feeling hung.
_MAX_ATTEMPTS = 5
_RETRY_DELAY_SECONDS = 1.0
_RETRY_BACKOFF = 2.0


def _retry_window():
    """Total seconds the retry loop spends sleeping — for the error message."""
    return int(sum(_RETRY_DELAY_SECONDS * (_RETRY_BACKOFF ** i)
                   for i in range(_MAX_ATTEMPTS - 1)))


def _http(method, url, data=None, token=None, body=None, content_type=None):
    """One request. Returns parsed JSON; raises ZohoError carrying Zoho's body.

    Pass either data (urlencoded for you) or a prebuilt body + content_type
    (the multipart path for file uploads).
    """
    if body is None:
        body = urllib.parse.urlencode(data).encode() if data else None
        content_type = "application/x-www-form-urlencoded" if body else None
    # Python's default User-Agent ("Python-urllib/3.x") reads as bot traffic
    # to some corporate network security tools, which can throttle or block
    # it under repeated hits even when isolated requests go through fine —
    # exactly the flaky pattern this was chasing. A normal one sidesteps that.
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HR-Onboarding-ZohoSync/1.0"}
    if token:
        headers["Authorization"] = "Zoho-oauthtoken " + token
    if body and content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=settings.ZOHO_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
            break
        except urllib.error.HTTPError as exc:
            # A real response from Zoho — retrying won't change a 4xx/5xx body.
            raw = exc.read().decode("utf-8", "replace")
            if exc.code == 404:
                raise ZohoError("404 %s\n%s" % (url, raw[:500]))
            raise ZohoError("HTTP %d %s\n%s" % (exc.code, url, raw[:1000]))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == _MAX_ATTEMPTS:
                # Naming the host matters: this exception (esp. "getaddrinfo
                # failed") gives no hint on its own which host it was trying
                # to reach — accounts.zoho.com (token refresh) and
                # people.zoho.com (everything else) fail identically otherwise.
                host = urllib.parse.urlsplit(url).hostname or url
                hint = (" — this machine could not resolve that hostname (DNS)"
                        if "getaddrinfo" in str(exc) else
                        " — this machine could not connect to that host")
                raise ZohoUnreachable(
                    "could not reach %s%s. Zoho was never contacted, so nothing "
                    "was written there. Tried %d times over %ds; check the "
                    "network/VPN and try again. (%s: %s)"
                    % (host, hint, attempt, _retry_window(), type(exc).__name__, exc))
            delay = _RETRY_DELAY_SECONDS * (_RETRY_BACKOFF ** (attempt - 1))
            log("http.retry", url=url, attempt=attempt, delay=delay, error=str(exc))
            time.sleep(delay)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ZohoError("non-JSON reply (HTTP %d) from %s:\n%s" % (status, url, raw[:500]))


def api(method, path, data=None, token=None, params=None, body=None, content_type=None):
    """Call a People API path, retrying without the leading /people on a 404.

    Zoho documents both /people/api/... and /api/..., and which one an org
    answers on varies - the Postman flow hit this too.
    """
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    base = settings.ZOHO_PEOPLE_BASE_URL.rstrip("/")
    try:
        return _http(method, "%s/people/api/%s%s" % (base, path, query),
                     data, token, body, content_type)
    except ZohoError as exc:
        if not str(exc).startswith("404 "):
            raise
        return _http(method, "%s/api/%s%s" % (base, path, query),
                     data, token, body, content_type)


def _multipart(fields, files):
    """multipart/form-data body: text fields plus (zoho_field, filename,
    content_type, path) file parts. Zoho's file-upload form fields only accept
    files this way - inputData alone cannot carry them."""
    boundary = "----hr-onboarding-" + os.urandom(12).hex()
    out = bytearray()
    for name, value in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                % (boundary, name)).encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, filename, ctype, path in files:
        with open(path, "rb") as fh:
            content = fh.read()
        safe = (filename or os.path.basename(path)).replace('"', "'")
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n"
                % (boundary, name, safe, ctype or "application/octet-stream")).encode()
        out += content + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode()
    return bytes(out), "multipart/form-data; boundary=" + boundary


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def exchange_code(code):
    """One-time: grant code -> refresh token. Replaces Postman request 0.

    The grant code from api-console.zoho.in > Self Client expires in minutes and
    is single-use. The refresh token in the reply is shown ONCE - it is printed
    here and never logged or cached; put it in .env yourself.
    """
    missing = [n for n in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET")
               if not getattr(settings, n)]
    if missing:
        raise ZohoError("set %s in .env first" % ", ".join(missing))

    payload = _http("POST", settings.ZOHO_ACCOUNTS_BASE_URL.rstrip("/") + "/oauth/v2/token", {
        "grant_type": "authorization_code",
        "client_id": settings.ZOHO_CLIENT_ID,
        "client_secret": settings.ZOHO_CLIENT_SECRET,
        "code": code,
    })
    if "refresh_token" not in payload:
        raise ZohoError(
            "no refresh_token in the reply: %s\n"
            "'invalid_code' means it expired or was already used - generate a new one. "
            "Tick the offline-access option in Self Client if the reply only has an "
            "access_token." % payload.get("error", payload))

    print("\nrefresh_token (shown once - paste into ZOHO_REFRESH_TOKEN in .env):\n")
    print("    %s\n" % payload["refresh_token"])
    scope = payload.get("scope", "?")
    print("scope granted: %s" % scope)
    if "forms.CREATE" not in scope:
        print("WARNING: no ZOHOPEOPLE.forms.CREATE - this token can read but not "
              "insert. Scopes cannot be added later; re-authorize to fix it.")


def access_token(force=False):
    """Cached access token, refreshed via the refresh token. ~1 hour lifetime."""
    # Stopgap: a hand-pasted access token, for when you do not have a refresh
    # token yet. No refresh is possible, so this dies after ~1 hour.
    if settings.ZOHO_ACCESS_TOKEN and not force:
        return settings.ZOHO_ACCESS_TOKEN

    if not force and os.path.exists(TOKEN_CACHE):
        try:
            with open(TOKEN_CACHE, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("expires_at", 0) > time.time() + 60:
                return cached["access_token"]
        except (OSError, ValueError, KeyError):
            pass

    missing = [n for n in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
               if not getattr(settings, n)]
    if missing:
        raise ZohoError("set %s in .env" % ", ".join(missing))

    payload = _http("POST", settings.ZOHO_ACCOUNTS_BASE_URL.rstrip("/") + "/oauth/v2/token", {
        "grant_type": "refresh_token",
        "client_id": settings.ZOHO_CLIENT_ID,
        "client_secret": settings.ZOHO_CLIENT_SECRET,
        "refresh_token": settings.ZOHO_REFRESH_TOKEN,
    })
    # Zoho reports auth failures as HTTP 200 with an "error" key.
    if "access_token" not in payload:
        raise ZohoError("token refresh failed: %s" % payload.get("error", payload))

    os.makedirs(OUT_DIR, exist_ok=True)
    token = payload["access_token"]
    with open(TOKEN_CACHE, "w", encoding="utf-8") as fh:
        json.dump({"access_token": token,
                   "expires_at": time.time() + int(payload.get("expires_in", 3600)),
                   "scope": payload.get("scope", "")}, fh)
    try:
        os.chmod(TOKEN_CACHE, 0o600)
    except OSError:
        pass
    print("token refreshed (scope: %s)" % payload.get("scope", "?"))
    return token


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def list_forms(token):
    payload = api("GET", "forms", token=token)
    forms = payload.get("response", {}).get("result", payload)
    print(json.dumps(forms, indent=2, ensure_ascii=False)[:20000])
    print("\nCopy the exact formLinkName of your target form into --form.")


def list_fields(token, form):
    payload = api("GET", "forms/%s/components" % urllib.parse.quote(form), token=token)
    result = payload.get("response", {}).get("result", payload)
    rows = _flatten_components(result)
    if not rows:
        # Response shapes vary by org - fall back to dumping what came back.
        print(json.dumps(result, indent=2, ensure_ascii=False)[:20000])
        return
    print("%-34s %-34s %-9s %s" % ("API NAME", "LABEL", "MANDATORY", "OPTIONS"))
    for row in rows:
        print("%-34s %-34s %-9s %s" % (
            row["api"][:34], row["label"][:34], row["mandatory"],
            ", ".join(row["options"])[:60]))
    print("\nPut the API NAME column into field_map.json. Picklists reject any "
          "value that is not an exact option above.")


def _flatten_components(node, out=None):
    """Pull {api,label,mandatory,options} out of Zoho's nested component tree."""
    out = [] if out is None else out
    if isinstance(node, dict):
        api_name = node.get("labelName") or node.get("apiName") or node.get("fieldName")
        if api_name and ("displayName" in node or "type" in node or "labelName" in node):
            opts = node.get("options") or node.get("optionList") or []
            if isinstance(opts, dict):
                opts = list(opts.values())
            out.append({
                "api": str(api_name),
                "label": str(node.get("displayName") or node.get("displayLabel") or ""),
                "mandatory": str(node.get("required", node.get("mandatory", ""))),
                "options": [str(o.get("value", o)) if isinstance(o, dict) else str(o)
                            for o in opts][:12],
            })
        for value in node.values():
            if isinstance(value, (dict, list)):
                _flatten_components(value, out)
    elif isinstance(node, list):
        for item in node:
            _flatten_components(item, out)
    return out


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

def find_by_email(token, form, email, email_field):
    """Existing records with this email. Raises on failure - callers must abort."""
    params = {
        "searchParams": json.dumps(
            {"searchField": email_field, "searchOperator": "Is", "searchText": email}),
        "sIndex": 1, "limit": 5,
    }
    payload = api("GET", "forms/%s/getRecords" % urllib.parse.quote(form),
                  token=token, params=params)
    result = payload.get("response", {}).get("result", [])
    if isinstance(result, dict):
        result = [result]
    return result or []


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def insert_record(token, form, payload, draft=False, files=None, tabular=None):
    data = {"inputData": _dumps(payload)}
    if tabular:
        # Tabular sections ride in their own request field, NOT inside
        # inputData (Zoho rejects that with 7013). Keyed by section link name.
        data["tabularData"] = _dumps(tabular)
    if draft:
        data["isDraft"] = "true"
    path = "forms/json/%s/insertRecord" % urllib.parse.quote(form)
    if files:
        body, ctype = _multipart(data, files)
        return api("POST", path, token=token, body=body, content_type=ctype)
    return api("POST", path, data=data, token=token)


def update_record(token, form, record_id, payload, files=None, tabular=None, draft=False):
    data = {
        "recordId": record_id,
        "inputData": _dumps(payload),
    }
    if tabular:
        data["tabularData"] = _dumps(tabular)
    if draft:
        # Keep the record a draft on update too: a non-draft update enforces
        # every mandatory field, including the tabular sections we don't write
        # yet (error 7052). As a draft it stays editable/valid in Zoho.
        data["isDraft"] = "true"
    path = "forms/json/%s/updateRecord" % urllib.parse.quote(form)
    if files:
        body, ctype = _multipart(data, files)
        return api("POST", path, token=token, body=body, content_type=ctype)
    return api("POST", path, data=data, token=token)


def get_records(token, form, limit=5):
    return api("GET", "forms/%s/getRecords" % urllib.parse.quote(form),
               token=token, params={"sIndex": 1, "limit": limit})


def record_id_from(response):
    """pkId out of {"response":{"result":{"pkId":...,"message":...},"status":0}}."""
    result = response.get("response", {}).get("result", {})
    if isinstance(result, list):
        result = result[0] if result else {}
    if isinstance(result, dict):
        for key in ("pkId", "recordId", "Zoho_ID"):
            if result.get(key):
                return str(result[key])
    return None


def has_errors(response):
    body = response.get("response", response)
    if isinstance(body, dict):
        if body.get("errors"):
            return True
        if body.get("status") not in (None, 0, "0"):
            return True
    return False


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def load_file_map():
    with open(os.path.join(HERE, "field_map.json"), encoding="utf-8") as fh:
        return json.load(fh).get("files", {}).get("map", {})


def load_candidate_documents(candidate_id, file_map):
    """The candidate's uploaded documents, resolved through the "files" map.

    Returns (attach, unmapped, missing): attach is (zoho_field, filename,
    content_type, path) ready for _multipart; unmapped are field keys that
    exist in the DB but have no Zoho field yet; missing are mapped documents
    whose file is gone from UPLOAD_DIR. Newest upload wins per field key.
    """
    from app.models import Document
    db = SessionLocal()
    try:
        docs = (db.query(Document).filter_by(candidate_id=candidate_id)
                .order_by(Document.id).all())
    finally:
        db.close()
    latest = {}
    for doc in docs:
        latest[doc.field_key] = doc
    attach, unmapped, missing = [], [], []
    for key in sorted(latest):
        doc = latest[key]
        zoho_field = file_map.get(key) or ""
        if not zoho_field:
            unmapped.append(key)
            continue
        path = os.path.join(settings.UPLOAD_DIR, doc.stored_filename)
        if not os.path.exists(path):
            missing.append(key)
            continue
        attach.append((zoho_field, doc.original_filename,
                       doc.content_type or "application/octet-stream", path))
    return attach, unmapped, missing


def load_candidate_payload(candidate_id, email):
    db = SessionLocal()
    try:
        q = db.query(Candidate)
        candidate = (q.filter_by(id=candidate_id).one_or_none() if candidate_id
                     else q.filter_by(email=email).one_or_none())
        if candidate is None:
            sys.exit("candidate not found")
        cemail = candidate.email or collect_values(db, candidate).get("email") or ""
        payload, tabular, skipped, unmapped, subform_counts = build_full_payload(db, candidate)
        return (candidate.id, cemail, payload, tabular, skipped, unmapped, subform_counts)
    finally:
        db.close()


def confirm(message, expected):
    print(message)
    try:
        typed = input("  type %s to continue: " % expected).strip()
    except EOFError:
        sys.exit("aborted: no terminal to confirm on - pass --yes if you are sure")
    if typed != expected:
        sys.exit("aborted")


def main():
    ap = argparse.ArgumentParser(
        description="Zoho People discovery and candidate push (no Postman, no sandbox).")
    ap.add_argument("--exchange-code", metavar="GRANT_CODE",
                    help="one-time: swap a Self Client grant code for a refresh token")
    ap.add_argument("--list-forms", action="store_true", help="discover form link names")
    ap.add_argument("--list-fields", action="store_true",
                    help="discover field API names for --form")
    ap.add_argument("--verify", action="store_true", help="read records back from --form")
    ap.add_argument("--candidate-id", type=int)
    ap.add_argument("--email", help="select the candidate by email instead of id")
    ap.add_argument("--form", help="target formLinkName. Required for any write.")
    ap.add_argument("--push", action="store_true",
                    help="actually write to Zoho. Without it nothing is sent.")
    ap.add_argument("--draft", action="store_true",
                    help="insert as a draft record you can delete. Use this first.")
    ap.add_argument("--record-id", help="update this record instead of inserting")
    ap.add_argument("--with-files", action="store_true",
                    help="attach the candidate's uploaded documents to the "
                         "file-upload fields mapped in field_map.json \"files\"")
    ap.add_argument("--email-field", default=DEFAULT_EMAIL_FIELD,
                    help="Zoho field searched for duplicates (default %s)" % DEFAULT_EMAIL_FIELD)
    ap.add_argument("--allow-duplicate", action="store_true",
                    help="push even if a record with this email already exists")
    ap.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    ap.add_argument("--show", action="store_true",
                    help="print the payload values, not just the field names")
    ap.add_argument("--limit", type=int, default=5, help="--verify page size (default 5)")
    args = ap.parse_args()

    # Reads may fall back to .env; writes never do.
    form = args.form or (None if args.push else settings.ZOHO_CANDIDATE_FORM or None)

    try:
        if args.exchange_code:
            exchange_code(args.exchange_code)
            return
        if args.list_forms:
            list_forms(access_token())
            return
        if args.list_fields:
            if not form:
                ap.error("--list-fields needs --form (or ZOHO_CANDIDATE_FORM in .env)")
            list_fields(access_token(), form)
            return
        if args.verify:
            if not form:
                ap.error("--verify needs --form (or ZOHO_CANDIDATE_FORM in .env)")
            print(json.dumps(get_records(access_token(), form, args.limit),
                             indent=2, ensure_ascii=False)[:20000])
            return

        if not args.candidate_id and not args.email:
            ap.error("pass --candidate-id or --email "
                     "(or one of --list-forms / --list-fields / --verify)")

        cid, cemail, payload, tabular, skipped, unmapped, subform_counts = load_candidate_payload(
            args.candidate_id, args.email)

        os.makedirs(OUT_DIR, exist_ok=True)
        out_path = os.path.join(OUT_DIR, "candidate_%d.inputData.json" % cid)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"inputData": payload, "tabularData": tabular}, fh,
                      ensure_ascii=False, separators=(",", ":"))

        print("candidate %d <%s>" % (cid, cemail))
        print("  %d flat fields in payload: %s" % (len(payload), ", ".join(sorted(payload))))
        print("  %d subform section(s): %s" % (
            len(subform_counts),
            ", ".join("%s=%d rows" % (s, n) for s, n in subform_counts.items()) or "-"))
        print("  %d empty in DB, omitted: %s" % (len(skipped), ", ".join(skipped) or "-"))
        print("  %d unmapped in field_map.json: %s" % (len(unmapped), ", ".join(unmapped) or "-"))
        print("  payload written to %s" % out_path)
        if args.show:
            print(json.dumps(payload, indent=2, ensure_ascii=False))

        files = []
        if args.with_files:
            files, files_unmapped, files_missing = load_candidate_documents(
                cid, load_file_map())
            print("  %d documents to attach: %s"
                  % (len(files), ", ".join(f[0] for f in files) or "-"))
            print("  %d documents with no Zoho field in field_map.json \"files\": %s"
                  % (len(files_unmapped), ", ".join(files_unmapped) or "-"))
            if files_missing:
                print("  WARNING: %d mapped documents missing on disk: %s"
                      % (len(files_missing), ", ".join(files_missing)))

        if not args.push:
            print("\ndry run - nothing sent to Zoho. "
                  "Add --push --form <formLinkName> to write.")
            return

        # ---- from here on we are writing to a real Zoho org ----
        if not args.form:
            sys.exit("--push requires an explicit --form; it is never read from .env")
        if not payload:
            sys.exit("refusing to push an empty payload - fill in field_map.json first")

        token = access_token()

        if not args.record_id:
            if not cemail:
                sys.exit("candidate has no email, so the duplicate check cannot run. "
                         "Use --record-id to update an existing record, or "
                         "--allow-duplicate if you accept the risk.")
            try:
                existing = find_by_email(token, args.form, cemail, args.email_field)
            except ZohoError as exc:
                # Failing open here is how you end up with two records per person.
                if not args.allow_duplicate:
                    sys.exit("duplicate check failed, refusing to insert:\n%s\n"
                             "Check --email-field against --list-fields, or pass "
                             "--allow-duplicate." % exc)
                existing = []
                print("WARNING: duplicate check failed, continuing on --allow-duplicate")
            if existing and not args.allow_duplicate:
                print(json.dumps(existing, indent=2, ensure_ascii=False)[:2000])
                sys.exit("a record with %s already exists on %s - insertRecord has no "
                         "dedupe. Update it with --record-id, or override with "
                         "--allow-duplicate." % (cemail, args.form))

        action = ("UPDATE record %s" % args.record_id if args.record_id
                  else "INSERT a %srecord" % ("DRAFT " if args.draft else ""))
        if not args.yes:
            confirm("\nAbout to %s on form '%s' in the LIVE Zoho org (%s)\n"
                    "  candidate %d <%s>, %d fields, %d documents"
                    % (action, args.form, settings.ZOHO_PEOPLE_BASE_URL,
                       cid, cemail, len(payload), len(files)),
                    args.form)

        log("push.request", form=args.form, candidate_id=cid, email=cemail,
            record_id=args.record_id, draft=args.draft, payload=payload,
            tabular=tabular, files=[f[0] for f in files])
        if args.record_id:
            response = update_record(token, args.form, args.record_id, payload,
                                     files=files, tabular=tabular)
        else:
            response = insert_record(token, args.form, payload, draft=args.draft,
                                     files=files, tabular=tabular)
        log("push.response", form=args.form, candidate_id=cid, response=response)

        print(json.dumps(response, indent=2, ensure_ascii=False)[:4000])
        if has_errors(response):
            sys.exit("Zoho reported an error - the errors block usually names the "
                     "offending field. Fix field_map.json or the value format and re-run.")
        new_id = args.record_id or record_id_from(response)
        print("\nOK. record id: %s" % (new_id or "?"))
        if new_id:
            print("verify with: python integrations/zoho/zoho_client.py "
                  "--verify --form %s" % args.form)
    except ZohoError as exc:
        log("error", message=str(exc))
        sys.exit("Zoho: %s" % exc)


if __name__ == "__main__":
    main()
