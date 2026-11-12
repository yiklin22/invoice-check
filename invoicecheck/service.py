"""Use cases: import orders, take uploads, process, review, export.

Every function takes a Session and commits its own work. Matching is
recomputed for all invoices after any change, because one invoice can
change another's result (cumulative quantities, duplicates).
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from . import config
from .extraction import extract_invoice, parse_decimal
from .matching import REQUIRED, match_all
from .models import (ACCEPTED, APPROVED, CLEAN, DISPUTED, NEEDS_REVIEW, OPEN, QUEUED, REJECTED,
                     UNREADABLE, Discrepancy, Evidence, Invoice, InvoiceLine, PurchaseOrder,
                     PurchaseOrderLine, ReviewDecision, UploadBatch, UploadItem)
from .schemas import PO, ExtractedLine, Finding, MatchInvoice, POLine

PO_COLUMNS = ["po_number", "supplier", "line_no", "sku", "description", "quantity", "unit_price"]
HEADER_FIELDS = ["supplier_name", "invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total"]
LINE_FIELDS = ["sku", "description", "quantity", "unit_price", "line_total"]
MATCH_LOCK = 4_815_162_342


class ReviewError(ValueError):
    """The request is not allowed in the invoice's current state, or is invalid."""


# --- purchase orders ---------------------------------------------------------

def import_purchase_orders(session: Session, text: str) -> dict:
    """Load a purchase-order CSV. All rows are checked first; one bad row
    rejects the whole file so a half-loaded order list cannot exist."""
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in PO_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return {"ok": False, "errors": [f"Missing column(s): {', '.join(missing)}"], "orders": 0, "lines": 0}
    errors, parsed = [], {}
    for n, row in enumerate(reader, 2):
        po = (row["po_number"] or "").strip()
        qty, price = parse_decimal(row["quantity"]), parse_decimal(row["unit_price"])
        line_no = (row["line_no"] or "").strip()
        problems = []
        if not po:
            problems.append("po_number is empty")
        if not (row["sku"] or "").strip():
            problems.append("sku is empty")
        if not line_no.isdigit():
            problems.append("line_no is not a whole number")
        if qty is None or qty <= 0:
            problems.append("quantity is not a positive number")
        if price is None or price < 0:
            problems.append("unit_price is not a number")
        if not problems:
            entry = parsed.setdefault(po, {"supplier": row["supplier"].strip(), "lines": {}})
            if entry["supplier"] != row["supplier"].strip():
                problems.append(f"{po} has two different suppliers")
            if int(line_no) in entry["lines"]:
                problems.append(f"{po} line {line_no} appears twice")
            entry["lines"][int(line_no)] = (row["sku"].strip(), row["description"].strip(), qty, price)
        errors += [f"Row {n}: {p}" for p in problems]
    if errors:
        return {"ok": False, "errors": errors, "orders": 0, "lines": 0}

    existing = {p.po_number: p for p in session.scalars(
        select(PurchaseOrder).options(selectinload(PurchaseOrder.lines)))}
    count = 0
    for number, entry in parsed.items():
        po = existing.get(number)
        if po is None:
            po = PurchaseOrder(po_number=number, supplier_name=entry["supplier"])
            session.add(po)
        po.supplier_name = entry["supplier"]
        current = {ln.line_no: ln for ln in po.lines}
        for line_no, ln in current.items():
            if line_no not in entry["lines"]:
                po.lines.remove(ln)
        for line_no, (sku, desc, qty, price) in entry["lines"].items():
            ln = current.get(line_no)
            if ln is None:
                ln = PurchaseOrderLine(line_no=line_no)
                po.lines.append(ln)
            ln.sku, ln.description, ln.quantity, ln.unit_price = sku, desc, qty, price
            count += 1
    session.flush()
    rematch_all(session)
    session.commit()
    return {"ok": True, "errors": [], "orders": len(parsed), "lines": count}


def load_pos(session: Session) -> dict[str, PO]:
    rows = session.scalars(select(PurchaseOrder).options(selectinload(PurchaseOrder.lines)))
    return {p.po_number: PO(po_number=p.po_number, supplier_name=p.supplier_name, lines=tuple(
        POLine(line_no=ln.line_no, sku=ln.sku, description=ln.description,
               quantity=ln.quantity, unit_price=ln.unit_price) for ln in p.lines)) for p in rows}


# --- upload and processing ---------------------------------------------------

def create_batch(session: Session, files: list[tuple[str, bytes]]) -> UploadBatch:
    """Record uploaded files. A file whose bytes were seen before points at
    the existing invoice; no second record is made."""
    batch = UploadBatch()
    session.add(batch)
    for name, data in files[:config.MAX_FILES_PER_UPLOAD]:
        name = (name or "unnamed.pdf")[:300]
        if not data:
            batch.items.append(UploadItem(filename=name, outcome="rejected_upload", detail="The file is empty."))
            continue
        if len(data) > config.MAX_UPLOAD_BYTES:
            batch.items.append(UploadItem(
                filename=name, outcome="rejected_upload",
                detail=f"Larger than the {config.MAX_UPLOAD_BYTES // 1_000_000} MB limit."))
            continue
        digest = hashlib.sha256(data).hexdigest()
        invoice = session.scalar(select(Invoice).where(Invoice.sha256 == digest))
        if invoice is not None:
            batch.items.append(UploadItem(
                filename=name, outcome="already_uploaded", invoice=invoice,
                detail=f"Identical to {invoice.filename}, uploaded earlier."))
            continue
        invoice = Invoice(sha256=digest, filename=name, pdf=data, size_bytes=len(data), status=QUEUED)
        session.add(invoice)
        session.flush()
        batch.items.append(UploadItem(filename=name, outcome="new", invoice=invoice))
    for name, _ in files[config.MAX_FILES_PER_UPLOAD:]:
        batch.items.append(UploadItem(
            filename=name[:300], outcome="rejected_upload",
            detail=f"More than {config.MAX_FILES_PER_UPLOAD} files in one upload."))
    session.commit()
    return batch


def process_queued(session: Session) -> int:
    """Read every queued file, then recompute matching."""
    queued = session.scalars(select(Invoice).where(Invoice.status == QUEUED).order_by(Invoice.id)
                             .with_for_update(skip_locked=True)).all()
    for inv in queued:
        result = extract_invoice(inv.pdf)
        inv.page_texts = list(result.page_texts)
        if not result.readable:
            inv.status, inv.unreadable_reason = UNREADABLE, result.reason
            continue
        inv.status, inv.extraction_method = NEEDS_REVIEW, result.method
        for name in HEADER_FIELDS:
            setattr(inv, name, getattr(result, name))
        inv.currency = result.currency or config.CURRENCY
        inv.lines = [InvoiceLine(**ln.model_dump()) for ln in result.lines]
        inv.evidence = [Evidence(**ev.model_dump()) for ev in result.evidence]
    session.flush()
    rematch_all(session)
    session.commit()
    return len(queued)


# --- matching ---------------------------------------------------------------

def is_readable(inv: Invoice) -> bool:
    return inv.extraction_method is not None or inv.has_manual_edits


def _to_match(inv: Invoice) -> MatchInvoice:
    return MatchInvoice(
        id=inv.id, label=inv.filename, readable=is_readable(inv),
        unreadable_reason=inv.unreadable_reason, rejected=inv.status == REJECTED,
        supplier_name=inv.supplier_name, invoice_number=inv.invoice_number,
        invoice_date=inv.invoice_date, po_number=inv.po_number,
        subtotal=inv.subtotal, tax=inv.tax, total=inv.total,
        lines=tuple(ExtractedLine(line_no=ln.line_no, sku=ln.sku, description=ln.description,
                                  quantity=ln.quantity, unit_price=ln.unit_price,
                                  line_total=ln.line_total) for ln in inv.lines))


def _evidence_findings(inv: Invoice) -> list[Finding]:
    """A value read by a template must be traceable to a line on a page."""
    if not is_readable(inv):
        return []
    out = []
    have = {(e.field, e.line_no) for e in inv.evidence if e.located or e.method.startswith("manual")}
    wanted = [(f, None) for f in REQUIRED if getattr(inv, f) is not None]
    wanted += [("line", ln.line_no) for ln in inv.lines]
    manual_lines = {e.line_no for e in inv.evidence if e.field.startswith("line.")}
    for fld, line_no in wanted:
        if (fld, line_no) not in have and line_no not in manual_lines - {None}:
            out.append(Finding(code="EVIDENCE_MISSING", severity="medium", line_no=line_no, key=fld,
                               message=f"The value of {fld if line_no is None else f'line {line_no}'} "
                                       f"could not be located on a page."))
    return out


def rematch_all(session: Session) -> None:
    # One recomputation at a time across all processes; released at commit or rollback.
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MATCH_LOCK})
    invoices = session.scalars(
        select(Invoice).where(Invoice.status != QUEUED).order_by(Invoice.id)
        .options(selectinload(Invoice.lines), selectinload(Invoice.discrepancies),
                 selectinload(Invoice.evidence))).all()
    findings = match_all([_to_match(i) for i in invoices], load_pos(session))
    for inv in invoices:
        new = {f.fingerprint: f for f in findings[inv.id] + _evidence_findings(inv)}
        old = {d.fingerprint: d for d in inv.discrepancies}
        for fp, row in old.items():
            if fp not in new:
                inv.discrepancies.remove(row)
        appeared = False
        for fp, f in new.items():
            row = old.get(fp)
            if row is None:
                row = Discrepancy(fingerprint=fp, code=f.code, line_no=f.line_no, state=OPEN)
                inv.discrepancies.append(row)
                appeared = True
            elif (row.expected, row.actual) != (f.expected, f.actual) and row.state != OPEN:
                row.state = OPEN        # the facts changed, so the old decision no longer applies
                appeared = True
            row.severity, row.message = f.severity, f.message
            row.expected, row.actual, row.impact = f.expected, f.actual, f.impact
        _refresh_status(inv, appeared)
    session.flush()


def _refresh_status(inv: Invoice, appeared: bool) -> None:
    if not is_readable(inv):
        inv.status = UNREADABLE
    elif inv.status == REJECTED:
        return
    elif inv.status == APPROVED:
        if appeared:
            inv.status = NEEDS_REVIEW
            inv.decisions.append(ReviewDecision(
                action="approval_invalidated", reviewer="system",
                note="A new discrepancy appeared after approval."))
    elif not inv.discrepancies and not inv.has_manual_edits:
        inv.status = CLEAN
    else:
        inv.status = NEEDS_REVIEW


# --- review -----------------------------------------------------------------

def get_invoice(session: Session, invoice_id: int) -> Invoice:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        raise LookupError(f"No invoice {invoice_id}")
    return inv


def _parse(field: str, text: str):
    text = (text or "").strip()
    if text == "":
        return None
    if field == "invoice_date":
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ReviewError(f"{field}: use the form YYYY-MM-DD, not {text!r}.") from None
    if field in ("subtotal", "tax", "total", "quantity", "unit_price", "line_total"):
        value = parse_decimal(text)
        if value is None:
            raise ReviewError(f"{field}: {text!r} is not a number.")
        return value
    return text


def _text(value) -> str | None:
    return None if value is None else str(value)


def _same(a, b) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b
    return a == b


def apply_edit(session: Session, invoice_id: int, reviewer: str, header: dict[str, str],
               lines: list[dict[str, str]], note: str = "") -> int:
    """Apply a reviewer's corrections. Each changed value is logged with its
    old and new value, marked as manually entered, and matching is re-run.
    `lines` items carry `line_no` ('' for a new line) and may carry
    `delete`. Returns the number of values changed."""
    inv = get_invoice(session, invoice_id)
    if inv.status == QUEUED:
        raise ReviewError("This file has not been processed yet.")
    method, changes = f"manual:{reviewer}", 0

    def record(field: str, old, new, line_no: int | None = None) -> None:
        nonlocal changes
        changes += 1
        label = field if line_no is None else f"line {line_no} {field}"
        inv.decisions.append(ReviewDecision(action="edit_field", field=label, old_value=_text(old),
                                            new_value=_text(new), note=note or None, reviewer=reviewer))
        ev_field = field if line_no is None else f"line.{field}"
        for ev in [e for e in inv.evidence if e.field == ev_field and e.line_no == line_no]:
            inv.evidence.remove(ev)
        inv.evidence.append(Evidence(field=ev_field, line_no=line_no, raw_text=_text(new),
                                     method=method, located=False))

    for field in HEADER_FIELDS:
        if field in header:
            new, old = _parse(field, header[field]), getattr(inv, field)
            if not _same(old, new):
                setattr(inv, field, new)
                record(field, old, new)

    by_no = {ln.line_no: ln for ln in inv.lines}
    for item in lines:
        raw_no = str(item.get("line_no") or "").strip()
        values = {f: _parse(f, item.get(f, "")) for f in LINE_FIELDS if f in item}
        if raw_no.isdigit() and int(raw_no) in by_no:
            ln = by_no[int(raw_no)]
            if item.get("delete"):
                inv.lines.remove(ln)
                record("deleted", f"{ln.sku} x {ln.quantity}", None, ln.line_no)
                continue
            for f, new in values.items():
                old = getattr(ln, f)
                if not _same(old, new):
                    setattr(ln, f, new)
                    record(f, old, new, ln.line_no)
        elif any(v is not None for v in values.values()):
            line_no = max(by_no, default=0) + 1
            ln = InvoiceLine(line_no=line_no, **values)
            inv.lines.append(ln)
            by_no[line_no] = ln
            for f, new in values.items():
                if new is not None:
                    record(f, None, new, line_no)

    if changes:
        inv.has_manual_edits = True
        inv.currency = inv.currency or config.CURRENCY
        if inv.status == APPROVED:
            inv.decisions.append(ReviewDecision(action="approval_invalidated", reviewer="system",
                                                note="Fields were edited after approval."))
        if inv.status in (APPROVED, REJECTED, CLEAN, UNREADABLE):
            inv.status = NEEDS_REVIEW
        session.flush()
        rematch_all(session)
    session.commit()
    return changes


def decide_discrepancy(session: Session, invoice_id: int, fingerprint: str, action: str,
                       reviewer: str, note: str = "") -> None:
    inv = get_invoice(session, invoice_id)
    if action not in ("accept", "dispute", "reopen"):
        raise ReviewError(f"Unknown action {action!r}.")
    if inv.status != NEEDS_REVIEW:
        raise ReviewError(f"Decisions can only be made while the invoice needs review (it is {inv.status}).")
    row = next((d for d in inv.discrepancies if d.fingerprint == fingerprint), None)
    if row is None:
        raise ReviewError("That discrepancy no longer exists; reload the page.")
    if action == "accept" and not note.strip():
        raise ReviewError("Say why the discrepancy is acceptable.")
    row.state = {"accept": ACCEPTED, "dispute": DISPUTED, "reopen": OPEN}[action]
    inv.decisions.append(ReviewDecision(action=f"{action}_discrepancy", fingerprint=fingerprint,
                                        note=note.strip() or None, reviewer=reviewer))
    session.commit()


def decide_invoice(session: Session, invoice_id: int, action: str, reviewer: str, note: str = "") -> None:
    inv = get_invoice(session, invoice_id)
    if action == "approve":
        if inv.status not in (NEEDS_REVIEW, CLEAN):
            raise ReviewError(f"An invoice that is {inv.status} cannot be approved.")
        undecided = [d for d in inv.discrepancies if d.state != ACCEPTED]
        if undecided:
            raise ReviewError(f"{len(undecided)} discrepancy(ies) are still open or disputed.")
        inv.status = APPROVED
    elif action == "reject":
        if inv.status in (QUEUED, REJECTED):
            raise ReviewError(f"An invoice that is {inv.status} cannot be rejected.")
        if not note.strip():
            raise ReviewError("Say why the invoice is rejected.")
        inv.status = REJECTED
    elif action == "reopen":
        if inv.status not in (APPROVED, REJECTED):
            raise ReviewError("Only an approved or rejected invoice can be reopened.")
        inv.status = NEEDS_REVIEW
    else:
        raise ReviewError(f"Unknown action {action!r}.")
    inv.decisions.append(ReviewDecision(action=f"{action}_invoice", note=note.strip() or None,
                                        reviewer=reviewer))
    session.flush()
    rematch_all(session)
    session.commit()


def delete_invoice(session: Session, invoice_id: int) -> None:
    """Remove a file and everything derived from it."""
    session.delete(get_invoice(session, invoice_id))
    session.flush()
    rematch_all(session)
    session.commit()


# --- export -----------------------------------------------------------------

def _latest_notes(inv: Invoice) -> dict[str, ReviewDecision]:
    return {d.fingerprint: d for d in inv.decisions if d.fingerprint}


def export_discrepancies_csv(session: Session) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["invoice_id", "file", "supplier", "invoice_number", "invoice_date", "po_number",
                "invoice_total", "invoice_status", "code", "severity", "line_no", "message",
                "expected", "actual", "impact", "decision", "decision_note", "decided_by"])
    for inv in session.scalars(select(Invoice).order_by(Invoice.id)):
        notes = _latest_notes(inv)
        for d in inv.discrepancies:
            last = notes.get(d.fingerprint)
            w.writerow([inv.id, inv.filename, inv.supplier_name, inv.invoice_number, inv.invoice_date,
                        inv.po_number, inv.total, inv.status, d.code, d.severity, d.line_no, d.message,
                        d.expected, d.actual, d.impact, d.state,
                        last.note if last and d.state != OPEN else None,
                        last.reviewer if last and d.state != OPEN else None])
    return out.getvalue()


def export_invoices_csv(session: Session) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["invoice_id", "file", "status", "supplier", "invoice_number", "invoice_date", "po_number",
                "subtotal", "tax", "total", "currency", "discrepancies", "open_discrepancies",
                "manually_edited", "unreadable_reason"])
    for inv in session.scalars(select(Invoice).order_by(Invoice.id)):
        w.writerow([inv.id, inv.filename, inv.status, inv.supplier_name, inv.invoice_number,
                    inv.invoice_date, inv.po_number, inv.subtotal, inv.tax, inv.total, inv.currency,
                    len(inv.discrepancies), sum(d.state == OPEN for d in inv.discrepancies),
                    inv.has_manual_edits, inv.unreadable_reason])
    return out.getvalue()
