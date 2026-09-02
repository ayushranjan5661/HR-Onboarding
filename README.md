# HR Onboarding System

Role-based candidate onboarding: HR invites a candidate → candidate fills the
CIF → HR reviews/edits/approves or rejects → on approval, BGV + Document
Collection forms unlock, pre-filled with the candidate's existing details →
HR reviews those and closes out onboarding.

## Stack
- Backend: FastAPI + PostgreSQL (SQLAlchemy), JWT auth, two independent login
  systems (HR vs Candidate).
- Frontend: static HTML/CSS/JS — two separate portals, no build step.
- Files (Aadhaar/PAN/mark sheets/resume/etc.) are stored on disk under
  `backend/uploads/`, referenced from Postgres.

## Project layout
```
backend/
  app/
    main.py            FastAPI app + CORS
    models.py           Candidate, HRUser, CandidateProfile, FormSubmission, Document,
                         DocumentSnapshot, FieldEditPermission, FieldEditLog
    form_definitions.py  which fields belong to CIF vs BGV vs Document Collection
    edit_access.py       which fields HR may unlock on a submitted form, and where they live
    routers/auth.py     HR + candidate login/logout/change-password
    routers/hr.py       invite, review, edit/delete fields, grant/revoke edit access,
                         audit trail, approve/reject
    routers/candidate.py  status, submit CIF/BGV/Document forms, apply granted edits
  init_db.py            creates tables + seeds first HR login
  requirements.txt
frontend/
  hr-portal/            login.html, dashboard.html, candidate.html
  candidate-portal/      login.html, change-password.html, home.html,
                          cif-form.html, bgv-form.html, document-form.html,
                          my-corrections.html
  js/field-labels.js     field labels shared by both portals
.env                     secrets (DB url, JWT key, seed HR login) — not committed
```

## One-time setup
1. Create the Postgres database referenced by `DATABASE_URL` in `.env`
   (default: `hr_onboarding`).
2. Install deps and seed the DB. **Use Python 3.11 or 3.12** — the
   pydantic/psycopg2 wheels don't have prebuilt binaries for 3.14 yet and
   fail to compile without Rust/C++ build tools (tested: 3.11 installs
   clean, 3.14 does not):
   ```
   cd backend
   C:\Users\ayush_r\AppData\Local\Programs\Python\Python311\python.exe -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python init_db.py
   ```
   This prints the seeded HR login (from `SEED_HR_EMAIL` / `SEED_HR_PASSWORD`
   in `.env`) — change the password after first login.

## Run it
```
cd backend
venv\Scripts\activate
uvicorn app.main:app --reload --port 8000
```
Serve the whole `frontend/` folder as ONE static site (single entry point):
```
cd frontend
python -m http.server 5500
```
Open **http://127.0.0.1:5500/index.html** — one login page with an HR /
Candidate role toggle.

**Ports must match in three places** — a mismatch shows up in the browser as
a bare "Failed to fetch" on login:

| What | Port | Where it's configured |
|---|---|---|
| Backend | 8000 | `uvicorn --port`, and `API_BASE` in `frontend/js/config.js` |
| Frontend | 5500 | `python -m http.server`, and `FRONTEND_ORIGINS` + `PORTAL_BASE_URL` in `.env` |

`frontend/js/config.js` is the single place the frontend's backend URL is
defined — every page loads it, so change it there only.

## Demo flow
1. Log in as HR (role toggle → HR) with the seeded HR login → **Invite
   Candidate**. HR gets a login ID, password, and a **one-click link**. The
   same details stay on the candidate's detail page, with **New Link** to
   reissue and **Delete Invitation** (on the dashboard list) to remove
   everything.
2. Candidate opens the one-click link — it signs them in and drops them
   straight into the **CIF** (a local duplicate of the Zoho form's fields,
   since we don't have API access to the live Zoho form). Email + password
   still works as a fallback.

### One-click invite links
The link looks like `index.html?token=<43 random chars>&next=form`.

- The token *is* a credential — anyone holding the link can fill that
  candidate's forms. Send it to the candidate only.
- It grants exactly what a password login grants: that one candidate's own
  forms, never HR access.
- It expires after `INVITE_LINK_EXPIRY_DAYS` (default 30) and dies instantly
  when HR clicks **New Link** or deletes the candidate.
- `next=form` picks whichever form is pending — CIF, then BGV, then Document
  Collection — so one link keeps working through the whole process. Once
  nothing is pending it lands on the status page instead.
- Expired or revoked links fall back to the normal login form with an
  explanation rather than a dead end.
3. Back in the HR portal, open the candidate → review every submitted field,
   edit or delete anything, then **Approve** or **Reject**.
4. On **Approve**, only the **Document Collection** form unlocks. Details
   already on file (name/email/contact/PAN/etc.) are pulled from the CIF, so
   the candidate fills only what is unique to that form.
5. Candidate submits their documents → HR reviews them. **Approving the
   Document Collection is what unlocks the BGV form** — the stages are
   sequential, so BGV cannot be started or submitted before then. Rejecting
   the documents leaves BGV locked.
6. Candidate submits BGV → HR approves it → HR marks onboarding complete.

### Form sequence
```
CIF  ──HR approves──▶  Document Collection  ──HR approves──▶  BGV  ──HR approves──▶  Complete
```
**Every form locks the moment it is submitted** — see below. A form that is
not yet its turn shows as LOCKED in both portals.

7. **Reject** at any gate stops the flow — the candidate portal shows the
   application as closed and no further form can be submitted.

### Correcting a submitted form
A submitted form is the record HR reviews, so the candidate cannot go back and
rewrite it. When something does turn out to be wrong — a mistyped Aadhaar
number, say — HR opens that **one value**, not the form:

1. HR portal → candidate → each submitted form card has **Allow Candidate
   Edit** → tick what needs correcting, optionally add a note explaining why.
2. The candidate sees *Corrections requested by HR* on their home page and
   changes only those values, in `my-corrections.html`, grouped by form. They
   fill in as many as they want, give **one reason for the submission** (HR
   opened the fields together, so they are not asked field by field), and
   submit once. Anything left blank stays open for later.
3. Each grant is single-use: submitting the change closes it again. HR can
   withdraw unused ones with **Revoke** on a single field or **Revoke all** in
   the banner header. Either asks for a reason and is itself recorded, so a
   field that was opened and then closed without an edit still has an
   explanation on file.
4. Every change — by HR or by the candidate — is written to `field_edit_log`
   with the value before, the value after, the reason, the timestamp and who
   made it. It is **HR-only**: they read it in the **Change History** card, and
   there is no candidate-facing endpoint for it. The log records internal
   actions too (invite links reissued, passwords reset), so it is not something
   to show the person it is about.

Everything saved in one action shares a `change_set_id` and shows as **one
entry** in the Change History: one HR *Save Changes* covering four fields is one
box with four lines, not four boxes, and *Revoke all* over three fields is one
box reading "Edit access withdrawn — 3 fields". Every batch applies in a single
transaction, so a partial failure changes nothing.

### Document versions
When a document is replaced or removed — by HR or by the candidate — the old
file is **no longer deleted**. A `document_snapshots` row pins it on disk and
the audit entry points at both sides, so HR can open **Previous file** and
**New file** straight from the Change History and compare them. A file that is
the "new" side of one change is the same snapshot row referenced as the "old"
side of the next, so a chain of replacements does not duplicate rows.

The trade-off is deliberate: superseded uploads accumulate in
`backend/uploads/` rather than being cleaned up, because you cannot show what a
document used to contain after deleting it. Deleting a candidate still removes
every file they own, archived versions included.

This covers every column of every form. Three kinds of value can be opened:

| Kind | What it is | Example |
| --- | --- | --- |
| `FIELD` | a column on a one-row detail table | Aadhaar Number, Passport Number |
| `DOCUMENT` | one uploaded file | Aadhar Card, Signed BGV Consent Form |
| `ROW_FIELD` | one column of one entry in a repeating section | *Employment #1 — Acme Ltd → Reason for Leaving* |

Repeating entries are listed individually, so HR opens one cell of one entry
rather than a whole table. The guard rails: a value can only be opened once
its form is submitted, the HR-only columns (`hr_candidate_id`,
`hr_candidate_email`) are never grantable, a column that does not belong to
the named section is rejected, and a row belonging to another candidate is
rejected.

## Notes on the two source forms you shared
- The **CIF** (`cif-form.html`) and **Document Collection** form
  (`document-form.html`) are local duplicates built for this demo — Zoho
  People and Recruber are third-party SaaS with their own auth/paid plans, so
  we can't embed or call their live endpoints without your account API keys.
  Field sets were matched to what those forms typically collect.
- The **BGV form** (`bgv-form.html`) was designed from standard Indian IT
  background-verification practice, not from a supplied vendor form. It asks
  *who can verify* each claim rather than repeating the CIF:
  consent & authorisation (including a separate "may we contact your current
  employer now?"), identity extras (passport/DL), address history, education
  with roll & registration numbers and study mode, employment with manager/HR
  contacts, payroll company and rehire eligibility, gaps over 60 days, two
  professional references, and the legal declarations (conviction, pending
  case, termination, disciplinary action, bond/non-compete, dual employment).
  Education and employment rows are pre-filled from the candidate's CIF.
  If your BGV vendor has a prescribed layout, adjust
  `backend/app/form_definitions.py` (BGV_* lists) and regenerate the form.

## Cross-form AI mapping agent

The three forms ask for many of the same things under different names — the
CIF's *"Candidate Profile Picture"* is the Document Collection's *"Passport
size photo"*. An agent works these equivalences out from the labels the
candidate sees, so nothing is entered or uploaded twice.

`backend/app/agents/field_mapper.py` decides, in this order:

1. **Curated** — hand-verified pairs that must always hold.
2. **LLM** — Azure OpenAI (`VITE_AZURE_OPENAI_*` in `.env`) reasons over the
   label lists and proposes the rest.
3. **Heuristic** — token-overlap matching, used whenever the LLM is
   unreachable, so a form never breaks because an API call failed.

Every LLM proposal is validated before use: both keys must exist in the
catalogue, the kinds must match, the source must be an *earlier* form, and
low-confidence guesses are dropped (`AI_MAPPING_MIN_CONFIDENCE`, default 0.75).
A bad model response therefore cannot inject a nonsense mapping.

**Never inherited** (`BLOCKED_TARGETS`): consents, declarations, signature-of-
agreement checkboxes and dates — these must be given afresh on each form —
plus BGV-specific evidence (Form 16, bank statement, police clearance).

What the agent does for the candidate:
- **Pre-fills fields** it matched, highlighted green with *"Filled from your
  CIF — edit if this is wrong"*. Always editable.
- **Carries documents forward.** A mapped upload left blank is satisfied by
  copying the file already on record; the copy is independent, so deleting one
  form's document never removes another's. A mandatory upload counts as
  satisfied if it can be carried over.
- **Names company sections** in the Document Collection form after the actual
  employers listed on the CIF, instead of "Previous company 1".

Inspect what it decided: `GET /hr/field-mappings` (add `?refresh=true` to
re-run after changing a form). Resolved mappings are cached in-process, so the
LLM is not called per request. Set `AI_MAPPING_ENABLED=false` in `.env` to run
on curated + heuristic rules only.

`backend/app/field_catalog.py` is generated — regenerate it with
`scratchpad/gen_catalog.py` after changing any form definition.

## Security notes
- Passwords are bcrypt-hashed; HR and candidate sessions use separate JWTs
  scoped by role, so an HR token cannot access candidate endpoints or vice
  versa.
- All secrets (DB URL, `JWT_SECRET_KEY`, seed HR password) live in `.env`,
  which is not checked into source control — set your own values before
  going beyond local demo use, especially `JWT_SECRET_KEY`.
- Uploaded documents are only reachable via authenticated download endpoints
  (HR can fetch any candidate's files; a candidate can only fetch their own).
