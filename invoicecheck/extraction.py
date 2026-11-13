"""Read an invoice PDF into typed fields with evidence.

invoice2data does the template matching. This module adds what it does not
give us: an explicit "unreadable" result with a reason, Decimal parsing from
the printed characters (invoice2data parses amounts to float), and the page
and text line each value was read from.
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from invoice2data import extract_data
from invoice2data.extract.loader import read_templates
from pdfminer.high_level import extract_text
from pdfminer.pdfpage import PDFPage

from .schemas import EvidenceItem, ExtractedInvoice, ExtractedLine

log = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).parent / "templates_i2d"
MIN_TEXT_CHARS = 40

# Words that usually sit on the same printed line as the value. Used only to
# choose between two lines that both contain the value.
HINTS = {
    "supplier_name": (),
    "invoice_number": ("Invoice", "INVOICE"),
    "invoice_date": ("Date", "date", "Issued"),
    "po_number": ("PO", "order"),
    "subtotal": ("Subtotal", "Sub-total", "Net"),
    "tax": ("GST",),
    "total": ("Total", "TOTAL", "Amount due"),
}


@lru_cache(maxsize=1)
def templates() -> list:
    return read_templates(str(TEMPLATE_DIR))


def parse_decimal(raw: object) -> Decimal | None:
    """Parse a printed amount such as "$1,234.50". None if it is not a number."""
    if raw is None:
        return None
    text = re.sub(r"[,$\s]", "", str(raw))
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _first(value: object) -> str | None:
    """invoice2data returns a list when a pattern matches more than once."""
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def read_pages(path: str) -> list[str]:
    with open(path, "rb") as fh:
        count = sum(1 for _ in PDFPage.get_pages(fh))
    return [extract_text(path, page_numbers=[i]) for i in range(count)]


def locate(pages: list[str], needle: str, hints: tuple[str, ...] = (),
           also: tuple[str, ...] = ()) -> tuple[int, int, str] | None:
    """Find the page and line that print `needle` (and every string in `also`)."""
    found = []
    for p, text in enumerate(pages, 1):
        for i, line in enumerate(text.splitlines(), 1):
            if needle in line and all(a in line for a in also):
                found.append((p, i, line.strip()))
    if not found:
        return None
    for hit in found:
        if any(h in hit[2] for h in hints):
            return hit
    return found[0]


def _evidence(pages, field, raw, method, line_no=None, also=()) -> EvidenceItem:
    hit = locate(pages, raw, HINTS.get(field, ()), also) if raw else None
    return EvidenceItem(field=field, line_no=line_no, raw_text=raw, method=method,
                        page=hit[0] if hit else None, page_line=hit[1] if hit else None,
                        line_text=hit[2] if hit else None, located=hit is not None)


def unreadable(reason: str, pages: list[str] | None = None) -> ExtractedInvoice:
    return ExtractedInvoice(readable=False, reason=reason, page_texts=tuple(pages or ()))


def extract_invoice(pdf: bytes, model_reader=None, scan_reader=None, max_scan_pages: int = 5) -> ExtractedInvoice:
    """`model_reader(pages) -> ExtractedInvoice`, when given, is tried on a
    file that has text but that no supplier template can read.
    `scan_reader(pdf) -> ExtractedInvoice`, when given, is tried on a file
    with no text layer and at most `max_scan_pages` pages."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf)
        tmp.flush()
        try:
            pages = read_pages(tmp.name)
        except Exception as exc:  # pdfminer raises many unrelated types on bad files
            return unreadable(f"The file could not be parsed as a PDF ({type(exc).__name__}).")
        if sum(len(re.sub(r"\s", "", p)) for p in pages) < MIN_TEXT_CHARS:
            if scan_reader is None:
                return unreadable("The PDF has no text layer. It is probably a scan or a photo.", pages)
            if not 0 < len(pages) <= max_scan_pages:
                return unreadable(f"The PDF has no text layer and {len(pages)} pages. Scans of more than "
                                  f"{max_scan_pages} pages are not sent to the model.", pages)
            return scan_reader(pdf)
        try:
            data = extract_data(tmp.name, templates=templates(), input_module="pdfminer")
        except Exception as exc:
            log.exception("invoice2data failed")
            return unreadable(f"Template extraction failed ({type(exc).__name__}).", pages)
    if not data:
        if model_reader is not None:
            return model_reader(pages)
        return unreadable("No supplier template matched, or a required field "
                          "(number, date, total) was not found.", pages)

    issuer = _first(data.get("issuer"))
    method = f"template:{issuer}"
    raw = {
        "supplier_name": issuer,
        "invoice_number": _first(data.get("invoice_number")),
        "invoice_date": _first(data.get("issued_raw")),
        "po_number": _first(data.get("po_number")),
        "subtotal": _first(data.get("subtotal_raw")),
        "tax": _first(data.get("tax_raw")),
        "total": _first(data.get("total_raw")),
    }
    evidence = [_evidence(pages, f, r, method) for f, r in raw.items()]

    parsed_date = data.get("date")
    invoice_date: date | None = parsed_date.date() if isinstance(parsed_date, datetime) else None

    lines = []
    for n, row in enumerate(data.get("lines") or [], 1):
        # invoice2data names these two columns `name` and `price_unit`.
        keys = {"sku": "sku", "description": "name", "quantity": "quantity",
                "unit_price": "price_unit", "line_total": "line_total"}
        cells = {ours: _first(row.get(theirs)) for ours, theirs in keys.items()}
        lines.append(ExtractedLine(
            line_no=n, sku=cells["sku"], description=cells["description"],
            quantity=parse_decimal(cells["quantity"]),
            unit_price=parse_decimal(cells["unit_price"]),
            line_total=parse_decimal(cells["line_total"])))
        also = tuple(c for c in (cells["unit_price"], cells["line_total"]) if c)
        evidence.append(_evidence(pages, "line", cells["sku"], method, line_no=n, also=also))

    return ExtractedInvoice(
        readable=True, method=method, page_texts=tuple(pages),
        supplier_name=issuer, invoice_number=raw["invoice_number"], invoice_date=invoice_date,
        po_number=raw["po_number"], subtotal=parse_decimal(raw["subtotal"]),
        tax=parse_decimal(raw["tax"]), total=parse_decimal(raw["total"]),
        currency=_first(data.get("currency")), lines=tuple(lines), evidence=tuple(evidence))
