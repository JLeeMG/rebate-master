# MacGear Rebate Master

The single place every MacGear customer rebate agreement and rate is held, by
entity, customer group and brand, with its full history, the evidence behind
each change, and who approved it. It is also the master of the registers those
agreements depend on: **customers, customer groups and brands**.

It was split out of the MacGear Forecasting Platform on 8 October 2026. The
forecasting platform reads customer groups, customers, brands and approved
rates from this platform's feed and never edits them. The specification is
*MacGear Forecasting Platform — Business and Technical Requirements* v1.1, §4.5
(the rebate master) and §5.3 (the agreement schema).

This README is the **operator guide**. It is written so someone who did not
build the platform can run it.

## Who uses it

| Person | Role | Can do |
|---|---|---|
| Jonathan Lee (CFO) | Administrator | Everything, including users, brand approvers and feed tokens |
| Siobhan | Rebate editor | Enters agreements and rate changes with their evidence; maintains customers, customer groups and brands. Cannot approve her own entries |
| Ken | Rebate reviewer | Approves or rejects rate changes entered by someone else |
| Group Product Managers / PMs (later) | Brand rebate approver | Approves rate changes for the brands assigned to them only |
| Raymond (later) | Viewer | Reads everything, changes nothing |

Viewers and brand approvers see rates but not the sales amounts recorded by
the legacy workbooks. The permission table is in `mgrm/auth/roles.py` and
pinned by `tests/test_auth.py`.

## Changing a rate: the audit trail

1. **Siobhan** opens the agreement and uses *Propose a rate change*. She must
   give the new rate, the date it starts, **the reason**, the source, and **at
   least one evidence file**: the email agreeing the rate saved as PDF, or the
   Outlook email itself (.msg), a saved email (.eml) or a screenshot. Files are
   checked by their content, not their name, and are limited to 20 MB each.
2. The rate is **proposed**. Nothing uses it yet.
3. **Ken** opens *Review rates*, opens the evidence, and approves or rejects
   (a rejection needs a note). He cannot approve a rate he entered; the
   database refuses it as well.
4. On approval the old rate is closed the day before the new one starts.
   Nothing is overwritten.
5. The **Change log** shows every change: who proposed it and when, the old
   and new rate, the reason, the evidence, who approved or rejected it, with
   what note and when. Each agreement's page shows its own log too.

What cannot be undone, by design and enforced by the database itself:

- **Evidence** is stored inside the database (so it is backed up with the
  rates), fingerprinted with a SHA-256 hash on upload, and can never be
  changed or deleted. To correct evidence, attach a further file.
- **A reviewed rate** cannot be changed or deleted; only an open-ended
  approved rate can be given an end date, once.
- **The audit log** is append-only.

Changing an agreement's details (customer group, brand, products covered,
type, basis, accrued or check-only) also needs a reason and may carry evidence.

**Brand approvers.** Under *Rebates → Brand approvers* the administrator can
assign a brand's Group Product Manager. From then on only they, or an
administrator, may approve that brand's rates; brands with no assigned
approver stay with Ken.

## The feed (for the forecasting platform)

Read-only, at `/api/v1/customer-groups`, `/api/v1/customers`, `/api/v1/brands`
and `/api/v1/rates` (approved rates only; `?as_of=YYYY-MM-DD` for those in
force on a date). Each caller needs a token, created under *Admin → Feed
tokens*. The token is shown once; only its fingerprint is stored. Put it in
the forecasting platform's `.env` as `REBATE_MASTER_TOKEN`. Revoke a token
there too. The field names are pinned by `tests/test_feed.py`; a change of
contract means a new version (`/api/v2`), not an edit.

## Everyday commands

In PowerShell, in this folder:

| To | Type |
|---|---|
| Start the platform | `.venv\Scripts\python.exe -m mgrm serve`, then open http://localhost:8001. The forecasting platform uses port 8000, so both can run at once. |
| Run the test suite | `.venv\Scripts\python.exe -m pytest` |
| Update the database after new code | `.venv\Scripts\python.exe -m mgrm migrate` |
| Add an administrator (first set-up, or if locked out) | `.venv\Scripts\python.exe -m mgrm create-admin` |
| Load a NetSuite register export | `.venv\Scripts\python.exe -m mgrm load customers <file.csv> --as <your email>` (or `classes`), or *Load data* in the platform |

## First-time installation

1. Python 3.12+, PostgreSQL and Git for Windows installed (see the
   forecasting platform's README).
2. In this folder:
   ```
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -e ".[dev]"
   powershell -ExecutionPolicy Bypass -File scripts\setup_database.ps1
   .venv\Scripts\python.exe -m mgrm migrate
   .venv\Scripts\python.exe -m mgrm create-admin
   .venv\Scripts\python.exe -m pytest
   ```

The rebate master has its own database (`mgrm`) and its own database account
(`mgrm_app`), separate from the forecasting platform's, so neither platform can
read or change the other's data except through the feed.

## What must not be edited by hand

- **`.env`** holds the database password and the sign-in signing key.
- **Evidence, reviewed rates and the audit log**: the database refuses.
- **`migrations/versions/*`** once committed: add a new migration instead.

## How the code is laid out

```
mgrm/
  data/        1  NetSuite customer and class registers; the one-time legacy workbook load;
                  the one-time import from the forecasting platform
  rebates/     2  agreements, rates, evidence, four-eyes review, rate cards, expiry
  api/         7  the read-only feed
  web/         7  the screens
  auth/           sign-in and roles
  domain/         currency, entities, periods
  checks/         label scanners and code scans
  models.py       database tables
migrations/       database structure changes, in order
tests/            the test suite
scripts/          one-off Windows set-up scripts
```

## Backups

Not yet in place. Until they are, the rebate master (including every piece of
evidence) lives only on this laptop.
