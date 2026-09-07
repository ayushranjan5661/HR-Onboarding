# Push HR Onboarding candidates into Zoho People

No Postman, no sandbox org. Everything runs from one CLI in this folder.

| File | Purpose |
|---|---|
| `zoho_client.py` | the CLI: token → discover forms → discover fields → insert → update → verify |
| `export_zoho_payload.py` | builds the `inputData` JSON for one candidate; also owns the payload builder the CLI reuses |
| `field_map.json` | local field key → Zoho field API name. Mostly blank until step 3. |
| `out/` | generated payloads, cached access token, push log — gitignored, contains PII |

## Setup

Fill these in the project `.env` (already gitignored — never commit them):

```
ZOHO_ACCOUNTS_BASE_URL=https://accounts.zoho.com   # .in / .eu — match your org's DC
ZOHO_PEOPLE_BASE_URL=https://people.zoho.com
ZOHO_CLIENT_ID=
ZOHO_CLIENT_SECRET=
ZOHO_REFRESH_TOKEN=
ZOHO_ACCESS_TOKEN=          # stopgap only, see below
ZOHO_CANDIDATE_FORM=        # read-only convenience; writes always need --form
```

Get the refresh token once from a **Self Client** at the API console of the
same DC (`api-console.zoho.com` / `api-console.zoho.in`):
Generate Code with scope
`ZOHOPEOPLE.forms.READ,ZOHOPEOPLE.forms.CREATE,ZOHOPEOPLE.forms.UPDATE`, then
swap that grant code for a refresh token (fill in client id/secret first):

```
python integrations/zoho/zoho_client.py --exchange-code <grant code>
```

The grant code expires in minutes and is single-use; the refresh token is
printed once — paste it into `ZOHO_REFRESH_TOKEN` yourself, it is never cached
or logged.

The write scopes matter: `forms.CREATE` for inserts, `.UPDATE` for `--record-id`.
`forms.READ` alone covers discovery and `--verify` only. Scopes cannot be added
to an existing refresh token — re-authorize to get a new one.

Access tokens are refreshed automatically and cached in `out/.token.json`
(~1 hour); no manual re-run when you get a 401.

If you only have a raw **access** token and no refresh token yet, put it in
`ZOHO_ACCESS_TOKEN` — it is then used as-is. It cannot be refreshed, so it 401s
after about an hour; it is for a quick `--list-forms` / `--list-fields` look
only. Clear it once `ZOHO_REFRESH_TOKEN` is set, or it keeps overriding it.

**Data centre:** both URLs must match the DC where the OAuth client was
created, or you get `invalid_client` or silent redirects.

## Without a sandbox

`insertRecord` writes a real record into whatever org the refresh token belongs
to. These guards stand in for a sandbox — don't disable them casually:

- nothing is sent without `--push`; the default run only builds the payload;
- `--push` requires an explicit `--form`, never read from `.env`, so a stale
  value cannot silently target the live Candidate form;
- before every insert, a `getRecords` lookup on the email field — a hit aborts,
  and a *failed lookup* also aborts (`insertRecord` has no dedupe, so running it
  twice creates two records);
- an interactive confirmation (type the form name) unless `--yes`;
- one candidate per run, no batch mode;
- every request and response appended to `out/zoho_push.log`.

Still do the first round-trip against a **duplicate form** (e.g.
`Candidate_Test`) and with `--draft`, so the record lands as a deletable draft.

## Steps

1. **Discover the form**

   ```
   python integrations/zoho/zoho_client.py --list-forms
   ```

   Copy the exact `formLinkName` of your target form.

2. **Discover the fields**

   ```
   python integrations/zoho/zoho_client.py --list-fields --form Candidate_Test
   ```

   Prints API name, label, mandatory flag and picklist options.

3. **Fill `field_map.json`** with those API names. Leave a value as `""` to skip
   that field. Mandatory Zoho fields with no local equivalent must be added to
   the payload by hand.

4. **Dry run** — builds the payload, sends nothing:

   ```
   python integrations/zoho/zoho_client.py --candidate-id 1 --show
   ```

   Reports which fields are mapped, empty, and still unmapped, and writes
   `out/candidate_1.inputData.json`.

5. **Push as a draft** to the test form (add `--with-files` to also attach the
   candidate's uploaded documents once the `"files"` map is filled in):

   ```
   python integrations/zoho/zoho_client.py --candidate-id 1 --form Candidate_Test --push --draft
   python integrations/zoho/zoho_client.py --candidate-id 1 --with-files --form Candidate_Test --push --draft
   ```

   A Zoho `errors` block usually names the offending field — fix
   `field_map.json` or the value format and re-run. The response's `pkId` is the
   record id.

6. **Correct a record** instead of inserting a second one:

   ```
   python integrations/zoho/zoho_client.py --candidate-id 1 --form Candidate_Test --push --record-id 1234
   ```

7. **Verify** what landed:

   ```
   python integrations/zoho/zoho_client.py --verify --form Candidate_Test
   ```

## Gotchas

- Date fields: Zoho normally expects `dd-MMM-yyyy` (e.g. `05-Jan-1998`). The
  local DB stores dates as free strings, so convert them in the map step if
  Zoho rejects them.
- Picklists (Gender, Marital Status, Source, Blood Group) reject any value that
  is not an exact configured option — `--list-fields` prints the valid ones.
- The dedupe search assumes the email field is `Email_ID`; if `--list-fields`
  shows a different name, pass `--email-field`.
- Documents/uploads: `--with-files` attaches the candidate's uploaded documents
  (photo, Aadhaar, PAN, marksheets, employment letters…) to file-upload fields,
  sent as one multipart request. Map each local document key to its Zoho field
  API name in the `"files"` map of `field_map.json` first — blank keys are
  skipped and listed in the dry run. Subform sections (education, work
  experience, dependents, previous employment) are not emitted — see
  `_subforms` in `field_map.json`.
- Zoho error codes: 7011 invalid form name · 7013 invalid field name · 7038
  permission denied (scope missing) · 7052 invalid or missing mandatory field.
- Rate limit: 100 requests/minute, then a 5-minute lock.

## Once it works

Only after the payload round-trips against the test form should this move into
the backend as a service call. `zoho_client.py`'s functions (`access_token`,
`insert_record`, `update_record`, `find_by_email`) are importable as-is, and the
mapping stays in `field_map.json` so the CLI and any backend code read the same
source.
