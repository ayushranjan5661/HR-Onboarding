# HR Onboarding System

Role-based candidate onboarding for LevelShift. HR invites a candidate → the
candidate fills the CIF → HR reviews / edits / approves or rejects → follow-up
forms (Document Collection, Declaration, Reference Check, BGV) are sent one at
a time, pre-filled from what the candidate already gave → HR closes out
onboarding and publishes the records to **Zoho People**.

## Stack
- **Backend:** FastAPI + PostgreSQL (SQLAlchemy), JWT auth, two independent
  login systems (staff vs candidate), plus token-only public links for
  referees and the Declaration form.
- **Frontend:** static HTML/CSS/JS, no build step. Portals: Super Admin
  (also used by Master Admin and Admin), Manager, HR, Candidate, Referee,
  Declaration.
- **AI:** Azure OpenAI for cross-form field mapping, CIF insights and
  reference-email drafting. Identity documents are checked by **local OCR
  only** (Tesseract), never sent to an AI service.
- **Integrations:** Zoho People (per-HR OAuth, push per form).
- **Hosting:** Azure App Service (F1) + Supabase Postgres — see
  [Deploy](#deploy-to-azure).

## Staff roles
One staff table (`hr_users`), one login (`/auth/staff/login`); the account's
role decides the landing page and what the server lets it see. Hierarchy:
`MASTER_ADMIN > SUPER_ADMIN > ADMIN > MANAGER > HR`.

| Role | Who | Sees | Can do |
| --- | --- | --- | --- |
| `MASTER_ADMIN` | developer account, seeded from `SEED_MASTER_*` in `.env` only | everything | everything below, plus create / deactivate / delete / reset Super Admins. Never created by the API, deleted or deactivated. Invisible to Super Admins |
| `SUPER_ADMIN` | seeded account (`SEED_HR_*`); more can be created by the Master Admin | every candidate | create / manage Admins, Managers and HR Executives, reset passwords, move HRs between teams, reassign any candidate, global audit |
| `ADMIN` | created by a Super Admin | every candidate | every Super Admin power over Managers, HRs, candidates and the audit trail — but never sees or touches Master, Super Admins or peer Admins |
| `MANAGER` | team lead | own + team candidates | create / deactivate / delete HRs in own team, invite for any team member, reassign within team, everything an HR can do on team candidates, team audit |
| `HR` | HR Executive | candidates assigned to them | invite, review, approve / reject, send / withdraw forms, edit access, Zoho push |

Rules enforced server-side (`app/scope.py`, `app/staff_service.py`):
- A candidate outside the caller's scope answers **404** on every `/hr` route,
  including those keyed by submission / document / row id.
- Ownership is `candidates.assigned_hr_id` (moves on reassignment);
  `created_by_hr_id` records who invited and never changes.
- Nobody can assign a candidate to a role ranked above them (`scope.ROLES_ABOVE`).
- Deleting a staff user is blocked while they own candidates or lead HRs.
  Reassign / move first, or deactivate.
- New staff get a generated password shown once; they must change it on first
  login (`must_reset_password` → every other call returns
  `403 PASSWORD_RESET_REQUIRED`).
- **No one sees a password a user chose** — not even the Master Admin. Only the
  generated one-time password is viewable, until it is changed.
- An HR with no Manager (rows from before teams existed) is visible only to
  the Super Admin until moved into a team.
- Staff-hierarchy changes are written to `staff_audit_log`.

## Candidate types and form sequence
HR picks the type when inviting; it decides which forms the candidate gets.

| Type (UI label) | Enum | Forms |
| --- | --- | --- |
| Permanent / Fulltime Consultant | `EXPERIENCED` | CIF → Document Collection → BGV, plus Declaration |
| Fresher / Trainee | `FRESHER` | CIF → Document Collection → Reference Check → BGV, plus Declaration |
| Consultant (On-Demand) | `ONDEMAND` | one On-Demand form only — no BGV / document flow |

```
CIF ─approve─▶ Document Collection ─approve─▶ [Reference Check] ─approve─▶ BGV ─approve─▶ Complete
        └─────▶ Declaration (sendable once the CIF is approved)
```
- **Nothing is sent automatically.** Approval makes the next form *sendable*;
  it stays LOCKED until HR presses **Send** (and can **Withdraw** it again).
- **Every form locks the moment it is submitted** — see
  [Correcting a submitted form](#correcting-a-submitted-form).
- **Reject** at any gate stops the flow; the candidate portal shows the
  application as closed.
- **Reference Check** (Freshers only): the candidate names referees; each
  referee gets a signed, login-free link to `referee form-portal/referee-form.html`.
  HR can draft the reference email with AI and download the Reference Check PDF.
- **Declaration** (undertaking for pending educational documents, with
  per-document expected dates): opened through its own signed link
  (`declaration-form-portal/`), no login. Reuses the CIF signature.
- HR can download generated PDFs for CIF, Document Collection, Reference
  Check, Declaration and On-Demand forms (`app/services/*_pdf.py`).

## Project layout
```
HR-Onboarding/
├─ backend/
│  ├─ app/
│  │  ├─ main.py               FastAPI app, CORS, router wiring; installs the DNS fallback first
│  │  ├─ config.py             settings read from ../.env
│  │  ├─ database.py           SQLAlchemy engine + session factory
│  │  ├─ deps.py               auth guards
│  │  ├─ security.py           password hashing, JWT, invite tokens, password encryption
│  │  ├─ scope.py / staff_service.py   staff hierarchy + candidate visibility
│  │  ├─ models.py             every table (see Data model)
│  │  ├─ schemas.py            Pydantic request / response models
│  │  ├─ form_definitions.py   which field belongs to which form and table
│  │  ├─ field_catalog.py      canonical field list + labels (generated)
│  │  ├─ edit_access.py        what HR may unlock on a submitted form
│  │  ├─ referee_links.py / declaration_links.py   signed login-free links
│  │  ├─ date_format.py        DD-MM-YYYY handling
│  │  ├─ routers/
│  │  │  ├─ auth.py            staff + candidate login / logout / change-password
│  │  │  ├─ admin.py           /admin — staff management + global audit
│  │  │  ├─ manager.py         /manager — team, reassignment, team audit
│  │  │  ├─ hr.py              invite, review, edit, send, decisions, PDFs, Zoho push
│  │  │  ├─ candidate.py       status, drafts, submission, granted edits
│  │  │  ├─ referee.py         public referee feedback form
│  │  │  ├─ declaration.py     public Declaration form
│  │  │  └─ zoho.py            per-HR Zoho connect / disconnect / OAuth callback
│  │  ├─ agents/
│  │  │  ├─ field_mapper.py    LLM — which fields mean the same thing across forms
│  │  │  ├─ prefill.py         mappings → pre-filled values + carried documents
│  │  │  ├─ insights.py        LLM — CIF summary and anomaly flags for HR
│  │  │  ├─ reference_email.py LLM — draft email to referees
│  │  │  ├─ doc_validator.py   local OCR + keyword scoring of uploads
│  │  │  ├─ doc_lexicon.py     keywords in every Indian script + every state board
│  │  │  └─ doc_check_queue.py background queue for document checks
│  │  ├─ services/
│  │  │  ├─ zoho_push.py       push each form to its Zoho People form
│  │  │  ├─ zoho_oauth.py      per-HR Zoho tokens (encrypted at rest)
│  │  │  └─ *_pdf.py           CIF / documents / reference / declaration / on-demand PDFs
│  │  └─ utils/
│  │     ├─ file_storage.py    upload validation, safe storage, snapshots
│  │     └─ dns_fallback.py    nslookup fallback when Python's DNS is blocked
│  ├─ tessdata/                Tesseract packs: English + 13 Indian languages (~30 MB)
│  ├─ tests/                   pytest: doc validator, DNS fallback, Zoho OAuth / push permissions
│  ├─ uploads/                 candidate files + retained versions (gitignored)
│  ├─ init_db.py               creates tables, light migrations, seeds Super / Master Admin
│  ├─ migrate_*.py             one-off data migrations
│  ├─ recheck_documents.py     re-score stored documents
│  ├─ requirements.txt
│  └─ .env.example
├─ frontend/                   static — serve this folder as ONE site
│  ├─ index.html               the only login page; Staff / Candidate toggle
│  ├─ change-password.html     forced first-login password change
│  ├─ js/                      shared: config.js (API_BASE), field labels, date format,
│  │                           staff-* pages, mobile nav, Zoho connect, welcome email
│  ├─ super-admin/             users, candidates, audit (Master / Super / Admin)
│  ├─ manager/                 team, candidates, audit
│  ├─ hr-portal/               dashboard + candidate.html (shared by every staff role)
│  ├─ candidate-portal/        home, CIF, Document Collection, BGV, Reference,
│  │                           On-Demand, my-corrections
│  ├─ referee form-portal/     public referee feedback form
│  └─ declaration-form-portal/ public Declaration form
├─ integrations/zoho/          Zoho CLI, payload builder, field maps (see its README)
├─ .claude/skills/deploy-azure/   Azure deploy skill + scripts
├─ .env                        secrets (NOT committed)
└─ README.md
```

### Data model
| Table | What it holds |
| --- | --- |
| `hr_users` | every staff login: `role`, `manager_id`, `must_reset_password`, one-time password, Zoho connection |
| `staff_audit_log` | staff create / deactivate / delete / reset / move, candidate reassignment |
| `candidates` | candidate login, stage, type, invite token, Zoho record ids |
| `candidate_profiles` | identity fields shared by every form |
| `form_submissions` | one row per form: status, submitted / reviewed timestamps |
| `cif_details` / `bgv_details` / `doc_collection_details` / `reference_check_details` / `declaration_details` / `ondemand_details` | each form's own flat fields |
| `education_details` / `employment_details` / `reference_details` | CIF repeating sections |
| `bgv_address_history` / `bgv_education_checks` / `bgv_employment_checks` / `bgv_reference_checks` / `bgv_gaps` | BGV repeating sections |
| `referee_feedback` | one row per referee response |
| `documents` | the file currently on record for each upload field, plus OCR verdict |
| `document_snapshots` | superseded files, kept so old versions stay openable |
| `form_drafts` / `form_draft_documents` | unsubmitted work; invisible to HR, deleted on submit |
| `field_edit_permissions` | one row per field HR has unlocked for a candidate |
| `field_edit_log` | audit trail: before, after, reason, when, who |
| `zoho_push_records` | what was pushed to Zoho, when, by whom |
| `experience_declaration_details` | retired form — kept for existing rows only |

## One-time setup
1. Create the Postgres database referenced by `DATABASE_URL` in the root
   `.env` (copy keys from `backend/.env.example`). Production uses Supabase.
2. Install deps and seed the DB. **Use Python 3.11 or 3.12** — pydantic /
   psycopg2 have no prebuilt wheels for 3.14 yet:
   ```
   python3.11 -m venv myenv
   myenv\Scripts\activate
   pip install -r backend\requirements.txt
   cd backend
   python init_db.py
   ```
   This seeds the Super Admin (`SEED_HR_*`) and, if `SEED_MASTER_PASSWORD` is
   set, the Master Admin. Change passwords after first login.
3. Install Tesseract for the document check:
   `winget install UB-Mannheim.TesseractOCR` (or set `TESSERACT_CMD`).

## Run it
```
cd backend
..\myenv\Scripts\activate
uvicorn app.main:app --port 8000
```
(Add `--reload` if you want auto-restart on edits.)

Serve the whole `frontend/` folder as ONE static site:
```
cd frontend
python -m http.server 5500
```
Open **http://127.0.0.1:5500/index.html** — one login page with a Staff /
Candidate toggle. Staff land in the portal for their role.

**Ports must match** — a mismatch shows as a bare "Failed to fetch" on login:

| What | Port | Where it's configured |
|---|---|---|
| Backend | 8000 | `uvicorn --port`, `API_BASE` in `frontend/js/config.js` |
| Frontend | 5500 | `python -m http.server`, `FRONTEND_ORIGINS` + `PORTAL_BASE_URL` in `.env` |

**DNS on this machine:** endpoint protection blocks Python's own DNS when a
script runs as a program (`getaddrinfo 11001`). `app/utils/dns_fallback.py`
falls back to `nslookup`; any new standalone script that calls Azure OpenAI or
Zoho must call `dns_fallback.install()` first.

## Demo flow
1. Log in as staff → **Invite Candidate**, pick the candidate type. HR gets a
   login ID, password and a **one-click link**; these stay on the candidate
   page with **New Link** to reissue and **Delete Invitation** to remove
   everything.
2. The candidate opens the link — it signs them in and lands on the pending
   form. Email + password still works as a fallback.
3. HR opens the candidate → reviews every field, edits or deletes anything,
   then **Approve** or **Reject**.
4. HR **Sends** each follow-up form in turn (see
   [sequence](#candidate-types-and-form-sequence)), reviewing each one.
5. HR marks onboarding complete and pushes the forms to Zoho People.

### One-click invite links
`index.html?token=<43 random chars>&next=form`
- The token *is* a credential — send it to the candidate only. It grants that
  one candidate's own forms, never staff access.
- Expires after `INVITE_LINK_EXPIRY_DAYS` (default 3) and dies instantly on
  **New Link** or candidate deletion.
- `next=form` picks whichever form is pending, so one link works through the
  whole process; once nothing is pending it lands on the status page.
- Expired / revoked links fall back to the login form with an explanation.

### Correcting a submitted form
A submitted form is the record HR reviewed, so the candidate cannot rewrite
it. HR opens **one value**, not the form:

1. Candidate page → form card → **Allow Candidate Edit** → tick what needs
   correcting, optionally add a note.
2. The candidate sees *Corrections requested by HR* and changes only those
   values in `my-corrections.html`, gives **one reason per submission**, and
   submits. Anything left blank stays open.
3. Each grant is single-use. HR can **Revoke** one field or **Revoke all**;
   either needs a reason and is recorded.
4. Every change — by HR or candidate — goes to `field_edit_log` (before,
   after, reason, time, who). **HR-only**: shown in the **Change History**
   card, never exposed to the candidate.

Changes saved in one action share a `change_set_id` and show as **one entry**;
each batch is a single transaction, so a partial failure changes nothing.

Three kinds of value can be opened:

| Kind | What it is | Example |
| --- | --- | --- |
| `FIELD` | a column on a one-row detail table | Aadhaar Number |
| `DOCUMENT` | one uploaded file | Aadhar Card |
| `ROW_FIELD` | one column of one repeating-section entry | *Employment #1 — Acme Ltd → Reason for Leaving* |

Guard rails: only submitted forms, HR-only columns (`hr_candidate_id`,
`hr_candidate_email`) are never grantable, and columns / rows must belong to
the named section and candidate.

### Document versions
A replaced or removed document is **not deleted**: a `document_snapshots` row
pins it on disk and the audit entry points at both sides, so HR can open
**Previous file** and **New file** from Change History. Superseded uploads
therefore accumulate in `backend/uploads/` by design; deleting a candidate
still removes every file they own, archived versions included.

## Cross-form AI mapping agent
The forms ask for many of the same things under different names (CIF
*"Candidate Profile Picture"* = Document Collection *"Passport size photo"*).
`backend/app/agents/field_mapper.py` resolves equivalences in this order:

1. **Curated** — hand-verified pairs that must always hold.
2. **LLM** — Azure OpenAI (`AZURE_OPENAI_*`; `VITE_`-prefixed names also
   accepted) proposes the rest.
3. **Heuristic** — token overlap, used whenever the LLM is unreachable.

Every LLM proposal is validated (keys exist, kinds match, source is an
*earlier* form, confidence ≥ `AI_MAPPING_MIN_CONFIDENCE`, default 0.75).
**Never inherited** (`BLOCKED_TARGETS`): consents, declarations, agreement
checkboxes and dates, and BGV-specific evidence.

For the candidate it pre-fills matched fields (highlighted, always editable),
carries documents forward as independent copies, and names company sections
after the employers listed on the CIF.

Inspect: `GET /hr/field-mappings` (`?refresh=true` to re-run). Results are
cached in-process. `AI_MAPPING_ENABLED=false` runs on curated + heuristic
only. `field_catalog.py` is generated — regenerate it after changing any form
definition.

## Document check (local OCR + keyword scoring)
`backend/app/agents/doc_validator.py` reads every upload and records
`ai_status`, `ai_doc_type`, `ai_confidence` (0-100), `ai_note` and
`ai_id_match` on the document row.

**Nothing leaves the server** — no AI service is involved:

| Step | What it does |
|---|---|
| Extract | PDF text layer when present; otherwise Tesseract OCR (`OCR_PRIMARY_LANGUAGES` first, then every pack in `tessdata` if not a clear match). `.docx` read directly. |
| Score | Weighted keyword / pattern rules from `doc_lexicon.py` — every Indian script, every state board and its exam names. Photos and signatures are scored from pixel statistics. |
| Decide | ≥ `DOC_VALIDATION_MATCH_SCORE` (80) → `MATCH`; < `DOC_VALIDATION_MISMATCH_SCORE` (40) → `MISMATCH`; between → `UNCERTAIN`. PAN / Aadhaar numbers are shape-checked (regex / Verhoeff) and compared with the profile. Unreadable → `UNVERIFIED`, always accepted. |

The stored note explains the verdict ("Looks like a PAN card (score 92%:
Income Tax Department, PAN-format number…)"). Runs on Save Draft, Submit,
granted re-uploads and HR Attach / Replace. **Verdicts are HR-only** (except
in `block` mode, where a refused upload must say why).

`DOC_VALIDATION_MODE`: `off`, `warn` (default — record for HR, never refuse)
or `block` (refuse a `MISMATCH`). Re-score old uploads with **Re-check
documents** on the candidate page, or `python recheck_documents.py [--all]`.

## Zoho People integration
- **Connect Zoho:** each HR links their own Zoho account (server-based OAuth
  app, `ZOHO_OAUTH_*`). Tokens are encrypted with `ZOHO_TOKEN_ENCRYPTION_KEY`.
  `ZOHO_AUTH_MODE=per_user` (default) pushes on the HR's own token;
  `admin` uses the shared `ZOHO_REFRESH_TOKEN`.
- **Push buttons** on the candidate page, one per target form, each enabled by
  its `ZOHO_*_WRITE_FORM` setting (blank = button refuses):
  Confirmation Process / Candidate, Documents (Experienced or Trainee),
  Reference Check, Declaration, On-Demand.
- First push inserts; later pushes update the same record. An email lookup
  runs before the first insert to avoid duplicates. Fields the HR's Zoho
  account can't see are dropped and reported. `ZOHO_PUSHED_BY_FIELD` records
  the pushing HR.
- **Documents push** needs a Pre-Induction record for the email; if missing,
  the portal creates one first (`ZOHO_PRE_INDUCTION_DEFAULTS`).
- Every request / response is logged to `integrations/zoho/out/zoho_push.log`
  (gitignored, contains PII).

CLI, field maps and form-schema notes: [integrations/zoho/README.md](integrations/zoho/README.md).

## Deploy to Azure
Frontend `HR-Onboarding-Portal` and backend `HR-Onboarding-Portal-API` share a
free F1 Linux plan in resource group `HR-Onboarding`; the database is
Supabase. Steps, app-settings sync and troubleshooting are in
[.claude/skills/deploy-azure/SKILL.md](.claude/skills/deploy-azure/SKILL.md).
Note: Group Policy blocks the `.ps1` scripts on this machine — run the
equivalent `az` commands directly.

## Tests
```
cd backend
..\myenv\Scripts\python.exe -m pytest tests -q
```

## Security notes
- Passwords are bcrypt-hashed; staff and candidate sessions use separate,
  role-scoped JWTs.
- All secrets live in the root `.env` (gitignored) and, in Azure, in app
  settings. Set your own `JWT_SECRET_KEY` before going beyond local use.
- Uploaded documents are reachable only via authenticated download endpoints
  (staff within scope; a candidate only their own).
- Referee and Declaration links are HMAC-signed, per-candidate tokens; they
  expose only that one form.
- ID documents are never sent to an external AI service.
