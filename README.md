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
    models.py           Candidate, HRUser, CandidateProfile, FormSubmission, Document, FieldEditLog
    form_definitions.py  which fields belong to CIF vs BGV vs Document Collection
    routers/auth.py     HR + candidate login/logout/change-password
    routers/hr.py       invite, review, edit/delete fields, approve/reject
    routers/candidate.py  status, submit CIF/BGV/Document forms
  init_db.py            creates tables + seeds first HR login
  requirements.txt
frontend/
  hr-portal/            login.html, dashboard.html, candidate.html
  candidate-portal/      login.html, change-password.html, home.html,
                          cif-form.html, bgv-form.html, document-form.html
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
Each form is editable by the candidate until HR reviews it; after review it
locks. A form that is not yet its turn shows as LOCKED in both portals.
7. **Reject** at any gate stops the flow — the candidate portal shows the
   application as closed and no further form can be submitted.

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

## Security notes
- Passwords are bcrypt-hashed; HR and candidate sessions use separate JWTs
  scoped by role, so an HR token cannot access candidate endpoints or vice
  versa.
- All secrets (DB URL, `JWT_SECRET_KEY`, seed HR password) live in `.env`,
  which is not checked into source control — set your own values before
  going beyond local demo use, especially `JWT_SECRET_KEY`.
- Uploaded documents are only reachable via authenticated download endpoints
  (HR can fetch any candidate's files; a candidate can only fetch their own).
