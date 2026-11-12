# Invoice check

Upload supplier invoice PDFs and a purchase-order CSV. The app reads each
invoice, compares it line by line with the order, and shows a reviewer what
does not agree, next to the place on the page where each value was read.

Each account is one business with one currency. Bank reconciliation is out
of scope.

**Status: the seven build milestones are done and tested on synthetic data,
and milestone 8 added login, per-account data, deletion, upload limits and
usage caps. It runs on this machine only: it is not hosted anywhere, and no
real invoice and no real user has touched it yet.** See
[What this does not show](#what-this-does-not-show).

## Result on the fixture set

20 invented invoices from three invented suppliers, each seeded with a known
condition. Output of `scripts/demo.py` against a running server
([docs/demo-run.txt](docs/demo-run.txt)):

| Seeded condition | Files | Surfaced as |
|---|---|---|
| Clean match (incl. a two-page invoice and a two-part delivery) | 7 | `clean`, no findings |
| PO written as "PO 1007" for PO-1007 | 1 | `AMBIGUOUS_PO`, held for review |
| Same invoice number sent twice | 1 | `DUPLICATE_INVOICE_NUMBER` |
| Same order billed again under a new number | 1 | `POSSIBLE_DUPLICATE`, `QTY_EXCEEDS_PO` |
| Unit price above / below the order | 2 | `PRICE_MISMATCH` |
| Line total is not quantity x price | 1 | `LINE_ARITHMETIC` |
| Quantity above the order (one invoice / across three invoices) | 2 | `QTY_EXCEEDS_PO` |
| Order number not in the list / no order number | 2 | `PO_NOT_FOUND`, `PO_MISSING` |
| Scanned image with no text / truncated file | 2 | `unreadable`, with the reason |
| Item not ordered, and total does not add up | 1 | `SKU_NOT_ON_PO`, `TOTAL_MISMATCH` |

20 of 20 cases came out as seeded, with no extra findings. Uploading the same
20 files again added no records. The fixtures are text PDFs I generated, so
this shows the matching rules work, not that extraction works on real paper.

The same run then checks the account boundary on the live server: a second
account's exports are empty, the first account's invoice, PDF and upload
answer 404 to it, a delete sent from another site answers 403, and deleting
both accounts ends their logins.

## Run it

Needs [uv](https://docs.astral.sh/uv/). No database install is needed: without
`DATABASE_URL` the app starts its own PostgreSQL from the `pgserver` package in
`./.pgdata`.

```bash
uv sync
uv run uvicorn invoicecheck.app:app --port 8811      # open http://127.0.0.1:8811 and create an account
uv run python scripts/demo.py http://127.0.0.1:8811  # in a second terminal: two throwaway accounts, deleted at the end
uv run pytest                                        # 111 tests, on a real PostgreSQL
```

To try it by hand, create an account and press **Load the sample data** on the
Account page.

To use your own server: `DATABASE_URL=postgresql://user:pass@host/db`, pointing
at an empty database. There are no migrations: the tables are created on first
start, and a database from before accounts existed is refused with a message.
The bundled PostgreSQL keeps this version's data in a database named
`invoicecheck_v2`; whatever an earlier version stored in `.pgdata` is left
alone and no longer shown.

## The screens

0. **Create an account / log in.** Everything below belongs to the account
   that is logged in. The **Account** page shows usage against each limit,
   loads the sample data, and deletes all data or the whole account (both ask
   for the password again).
1. **Upload** on the home page: invoice PDFs, and the purchase-order CSV.
2. **Processing**: one row per file, updating until every file is checked.
3. **Review**: the PDF and the text as read on the left; findings, extracted
   values and decisions on the right. Each value links to the page and line
   it came from. Values typed by a person are labelled with the name on
   their account.
4. **Corrections**: every field and line is editable. Saving re-runs matching
   and logs old and new values.
5. **Decisions**: accept (with a reason) or dispute each finding, then approve
   or reject the invoice.
6. **Export**: one CSV of findings with decisions, one CSV of invoices.

The layout is one column on a phone and two columns from 1000 px.

## Acceptance claims and the tests behind them

| Claim | Test |
|---|---|
| Every seeded discrepancy is surfaced, and nothing else | `test_service.py::test_every_seeded_discrepancy_is_surfaced_and_nothing_else`, `test_web.py::test_upload_to_export_flow` |
| Unreadable inputs never appear clean, and cannot be approved | `test_service.py::test_unreadable_inputs_never_appear_clean`, `test_web.py::test_unreadable_page_says_so_and_cannot_be_approved` |
| Re-uploading creates no duplicate records | `test_service.py::test_reupload_creates_no_duplicate_records`, `test_web.py::test_reupload_points_at_existing_records` |
| The result does not depend on upload order | `test_service.py::test_result_does_not_depend_on_upload_order` |
| Every extracted value is traceable to a page and line | `test_extraction.py::test_every_extracted_value_has_located_evidence` |
| An edit after approval withdraws the approval | `test_service.py::test_edit_after_approval_invalidates_it` |
| Money is Decimal end to end | `test_service.py::test_money_is_stored_as_decimal`, `test_matching.py::test_cent_rounding_is_half_up_on_decimals` |
| Nothing is readable or changeable without a login | `test_accounts.py::test_every_page_needs_a_login` |
| One account cannot read or change another's invoices, orders, uploads or exports | `test_accounts.py::test_another_account_sees_none_of_it`, `test_service_refuses_another_accounts_invoice` |
| One account's files and orders never change another's findings | `test_accounts.py::test_the_same_files_in_two_accounts_do_not_affect_each_other`, `test_an_order_in_one_account_does_not_match_an_invoice_in_another` |
| Deleting data or an account removes all of it, and only that account's | `test_accounts.py::test_delete_all_data_needs_the_password_and_leaves_the_account`, `test_delete_account_removes_everything_and_the_login` |
| Caps hold: files held, storage, new files per day, order lines, request size | `test_accounts.py::test_cap_on_files_held`, `test_cap_on_storage`, `test_daily_cap_is_not_reset_by_deleting`, `test_cap_on_order_lines_rejects_the_whole_file`, `test_oversized_and_unsized_requests_are_refused_unread` |
| Repeated wrong passwords lock the login for a while; logout and expiry end a session | `test_accounts.py::test_wrong_password_then_throttle`, `test_logout_and_expiry_end_the_session` |
| A form on another site cannot act with a visitor's login | `test_accounts.py::test_writes_from_another_site_are_refused` |

For the account tests I broke each rule on purpose, one at a time (invoice
lookup, batch lookup, the home page list, the orders and the invoices that
matching reads, the cross-site check), and each time at least one test failed.

## Design decisions

- **A near match never passes an invoice.** Orders and SKUs match exactly or
  not at all. "PO 1007" for "PO-1007" is reported with the candidate and waits
  for a person. Rejected alternative: fuzzy matching with a score, which turns
  a wrong guess into a silently approved payment.
- **"Unreadable" is a state, not an empty invoice.** A file with no text
  layer, a broken file, or a layout no template knows gets one finding and a
  reason. It has no fields, cannot be approved, and never counts as checked.
- **Clean is only ever automatic.** An invoice is `clean` when a template read
  every required value, each value was found on a page, and matching found
  nothing. Once a person edits any value the invoice needs an explicit
  approval, because the system can no longer vouch for the values.
- **Quantities are cumulative in invoice-date order.** Three invoices against
  one order line are judged together, and the answer is the same whatever
  order the files arrive in. Rejected invoices do not use up order quantity.
- **Reviewer decisions survive re-matching.** Each finding has a stable
  fingerprint. Matching is recomputed for all invoices after any change; a
  decision is kept only while the expected and actual values are unchanged.
- **The file hash is the identity.** SHA-256 of the bytes is unique within an
  account, so the same file is one record however often it is uploaded. The
  same invoice in a different file is caught by matching instead.
- **The account is checked twice.** Every table that holds business data has
  an `account_id`. The web layer answers 404 for another account's invoice or
  upload, the same as for one that does not exist, and the service functions
  take the account as an argument and check it again. Matching, duplicates and
  cumulative quantities are computed inside one account.
- **Sessions are random tokens kept server-side.** The cookie holds the token
  and the database holds its SHA-256, so logout really ends a session and a
  copy of the database does not let anyone log in. Passwords are scrypt hashes
  from the standard library. Rejected alternative: a signed cookie, which
  cannot be revoked. No new dependency was added for any of this.
- **Cross-site writes are refused by where the request came from.** Every
  POST is checked against `Sec-Fetch-Site`, or `Origin` when that is absent,
  and the cookie is `SameSite=Lax`. Rejected alternative: a hidden token in
  every form, which is more code in every template for the same result in
  current browsers.
- **Caps count what is held and what was done.** Files held and storage go
  down when files are deleted. The daily count of new files does not, so
  uploading and deleting in a loop is still capped.
- **Deleting is real.** Rows are removed, not hidden, and everything derived
  from them goes too through `ON DELETE CASCADE`.
- **Decimal from the printed characters.** invoice2data parses amounts to
  float. The templates also capture the raw text, and this code parses that to
  `Decimal`; PostgreSQL stores `NUMERIC`.
- **PostgreSQL only.** Tests run on PostgreSQL too, so row locks
  (`FOR UPDATE SKIP LOCKED` on the queue) and the advisory lock around
  matching are exercised, not mocked.

## What invoice2data gave, and what it did not

Evaluated on the fixtures (milestone 4): with three hand-written templates it
read all 144 header and line values of the 18 readable invoices correctly, and
returned nothing for the two unreadable ones. Model extraction was therefore
not needed for this set and **is not implemented**.

Things I had to work around:

- Amounts come back as float, so raw text is captured in extra fields.
- Line columns named `description` and `unit_price` are renamed to `name` and
  `price_unit`.
- It returns `{}` both when no template matches and when a required field is
  missing, so the two cases share one "unreadable" reason.
- It gives values but not positions, so evidence is located afterwards by
  searching the page text for the printed characters.

## Dependencies checked before use

Versions are pinned in `uv.lock`. Licenses are from each package's metadata.

| Package | Version | License | Used for |
|---|---|---|---|
| fastapi | 0.162.5 | MIT | web framework |
| uvicorn | 0.54.0 | BSD-3-Clause | server |
| sqlalchemy | 2.1.8 | MIT | database access |
| psycopg | 3.3.9 | LGPL-3.0-only | PostgreSQL driver (used as a library, unmodified) |
| pydantic | 2.14.3 | MIT | typed values between layers |
| jinja2 | 3.1.6 | BSD | server-rendered pages |
| python-multipart | 0.0.33 | Apache-2.0 | file uploads |
| invoice2data | 1.0.1 | MIT | template extraction |
| pdfminer.six | 20260107 | MIT | page text |
| pgserver | 0.1.4 | Apache-2.0 | bundled PostgreSQL for development and tests |
| reportlab, pillow (dev) | 5.1.0 | BSD, MIT-CMU | generating the fixture PDFs |

## What this does not show

- **Real documents.** Every invoice is synthetic, with a clean text layer and
  a layout I chose. Real supplier invoices need a template each, and scans
  need OCR or a model, neither of which is here.
- **Real users.** Nobody but me has used the screens. Correction counts and
  completion times do not exist yet.
- **A hosted deployment.** Everything ran on one machine over plain HTTP.
  HTTPS, a proxy, backups and monitoring are not set up, and nobody but me has
  reviewed the access control. I have not seen any screen in a browser; this
  machine has none, so the pages are tested over HTTP only.
- **The rest of account handling.** No email verification, no password reset,
  no way to change a password, and one login per account (no teams or roles).
  A user who forgets the password loses the account.
- **Protection from a hostile file.** Size and count are capped, but a small
  PDF built to make the reader slow would hold up the queue for every account,
  because one worker reads all of them in order.
- **Scale.** Matching is recomputed for every invoice in an account after each
  change. That is fine for hundreds of invoices and wrong for hundreds of
  thousands.
- **Schema changes.** There are no migrations, so changing a table means a new
  empty database.
- **Other rules.** No tax-rate check, no price tolerance, no goods-received
  matching, one currency.

## Limits and settings for a hosted demo

All are environment variables; the defaults are in `invoicecheck/config.py`.

| Variable | Default | Meaning |
|---|---|---|
| `INVOICECHECK_INVITE_CODE` | empty | When set, creating an account needs this code. **Set it before anyone else can reach the server**; without it signup is open. |
| `INVOICECHECK_MAX_ACCOUNTS` | 25 | Accounts that can exist |
| `INVOICECHECK_MAX_UPLOAD_BYTES` | 5 MB | One PDF |
| `INVOICECHECK_MAX_FILES` | 50 | Files in one upload |
| `INVOICECHECK_MAX_REQUEST_BYTES` | 30 MB | One request, refused from its `Content-Length` before the body is read |
| `INVOICECHECK_MAX_ORDER_FILE_BYTES` | 1 MB | The purchase-order CSV |
| `INVOICECHECK_MAX_INVOICES` | 200 | Files an account holds at one time |
| `INVOICECHECK_MAX_STORAGE_BYTES` | 100 MB | PDF bytes an account holds at one time |
| `INVOICECHECK_MAX_FILES_PER_DAY` | 100 | New files an account can add per UTC day |
| `INVOICECHECK_MAX_ORDER_LINES` | 5,000 | Purchase-order lines an account holds |
| `INVOICECHECK_SESSION_DAYS` | 14 | How long a login lasts |
| `INVOICECHECK_COOKIE_SECURE` | off | `1` forces the `Secure` cookie flag. A request that arrives as HTTPS gets it anyway. |

A login is refused for 15 minutes after 5 wrong passwords for one email, or 30
from one address. Behind a proxy, run uvicorn with `--proxy-headers` so the
address and the HTTPS scheme are the visitor's, not the proxy's.

## Next steps

1. Put it on a host with HTTPS and an invite code. This needs a choice of
   host and a PostgreSQL there; nothing in the code depends on which.
2. Password reset and email verification, which need a way to send email.
3. Model extraction for files that no template reads, server-side, with the
   same evidence rule: a value that cannot be found on the page is flagged.
   Count model calls against a per-account daily cap like uploads.
4. Three operators completing a real task, measuring corrections and time.

## Layout

```
invoicecheck/
  models.py       tables (accounts, NUMERIC money, unique file hash, decision log)
  auth.py         passwords, login sessions, login throttling
  config.py       limits and caps
  schemas.py      typed values passed between layers
  extraction.py   PDF -> fields + evidence, or "unreadable" with a reason
  templates_i2d/  one invoice2data template per supplier
  matching.py     the rules; pure functions, no database
  service.py      import, upload, process, edit, decide, delete, export; all per account
  app.py, web/    FastAPI routes, pages, stylesheet
fixtures/         generate.py, 20 PDFs, purchase_orders.csv, manifest.json
scripts/demo.py   drives a running server through the whole flow with two accounts
tests/            111 tests
```
