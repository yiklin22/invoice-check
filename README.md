# Invoice check

Upload supplier invoice PDFs and a purchase-order CSV. The app reads each
invoice, compares it line by line with the order, and shows a reviewer what
does not agree, next to the place on the page where each value was read.

It is built for one business and one currency. Bank reconciliation is out of
scope.

**Status: the seven build milestones are done and tested on synthetic data.
No real invoice and no real user has touched it yet.** See
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

## Run it

Needs [uv](https://docs.astral.sh/uv/). No database install is needed: without
`DATABASE_URL` the app starts its own PostgreSQL from the `pgserver` package in
`./.pgdata`.

```bash
uv sync
uv run uvicorn invoicecheck.app:app --port 8811      # open http://127.0.0.1:8811
uv run python scripts/demo.py http://127.0.0.1:8811  # in a second terminal: loads the fixtures
uv run pytest                                        # 89 tests, on a real PostgreSQL
```

To use your own server: `DATABASE_URL=postgresql://user:pass@host/db`.

## The screens

1. **Upload** on the home page: invoice PDFs, and the purchase-order CSV.
2. **Processing**: one row per file, updating until every file is checked.
3. **Review**: the PDF and the text as read on the left; findings, extracted
   values and decisions on the right. Each value links to the page and line
   it came from. Values typed by a person are labelled with their name.
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
- **The file hash is the identity.** SHA-256 of the bytes is unique in the
  database, so the same file is one record however often it is uploaded. The
  same invoice in a different file is caught by matching instead.
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
- **Access control.** There is no login. The reviewer name is a cookie anyone
  can set. Do not put it on the internet as it is.
- **Scale.** Matching is recomputed for every invoice after each change. That
  is fine for hundreds of invoices and wrong for hundreds of thousands.
- **Other rules.** No tax-rate check, no price tolerance, no goods-received
  matching, one currency.

## Next steps

1. Login, per-account data, deletion and usage caps, then a hosted demo.
2. Model extraction for files that no template reads, server-side, with the
   same evidence rule: a value that cannot be found on the page is flagged.
3. Three operators completing a real task, measuring corrections and time.

## Layout

```
invoicecheck/
  models.py       tables (NUMERIC money, unique file hash, decision log)
  schemas.py      typed values passed between layers
  extraction.py   PDF -> fields + evidence, or "unreadable" with a reason
  templates_i2d/  one invoice2data template per supplier
  matching.py     the rules; pure functions, no database
  service.py      import, upload, process, edit, decide, export
  app.py, web/    FastAPI routes, pages, stylesheet
fixtures/         generate.py, 20 PDFs, purchase_orders.csv, manifest.json
scripts/demo.py   drives a running server through the whole flow
tests/            89 tests
```
