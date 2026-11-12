"""Extraction is checked field by field against the fixture manifest."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from invoicecheck.extraction import extract_invoice, locate, parse_decimal

FIXTURES = Path(__file__).parent.parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())["invoices"]
READABLE = [m for m in MANIFEST if m["readable"]]
UNREADABLE = [m for m in MANIFEST if not m["readable"]]


def read(name: str):
    return extract_invoice((FIXTURES / "invoices" / name).read_bytes())


@pytest.mark.parametrize("entry", READABLE, ids=lambda m: m["file"])
def test_every_field_matches_ground_truth(entry):
    got, want = read(entry["file"]), entry["expected_fields"]
    assert got.readable
    assert got.supplier_name == want["supplier_name"]
    assert got.invoice_number == want["invoice_number"]
    assert got.invoice_date.isoformat() == want["invoice_date"]
    assert got.po_number == want["po_number"]
    for name in ("subtotal", "tax", "total"):
        value = getattr(got, name)
        assert isinstance(value, Decimal) and value == Decimal(want[name])
    assert len(got.lines) == len(want["lines"])
    for line, exp in zip(got.lines, want["lines"]):
        assert (line.sku, line.description) == (exp["sku"], exp["description"])
        assert line.quantity == Decimal(exp["quantity"])
        assert line.unit_price == Decimal(exp["unit_price"])
        assert line.line_total == Decimal(exp["line_total"])


@pytest.mark.parametrize("entry", READABLE, ids=lambda m: m["file"])
def test_every_extracted_value_has_located_evidence(entry):
    got = read(entry["file"])
    present = {e.field for e in got.evidence if e.raw_text}
    assert {"supplier_name", "invoice_number", "invoice_date", "total"} <= present
    for ev in got.evidence:
        if ev.raw_text:
            assert ev.located and ev.page >= 1 and ev.raw_text in ev.line_text, ev
    assert sum(e.field == "line" for e in got.evidence) == len(got.lines)


@pytest.mark.parametrize("entry", UNREADABLE, ids=lambda m: m["file"])
def test_unreadable_files_say_why_and_carry_no_fields(entry):
    got = read(entry["file"])
    assert not got.readable and got.reason
    assert got.invoice_number is None and got.total is None and not got.lines


def test_totals_on_page_two_are_cited_to_page_two():
    got = read("06_clean_bluegum_two_pages.pdf")
    pages = {e.field: e.page for e in got.evidence}
    assert pages["invoice_number"] == 1 and pages["total"] == 2


def test_text_from_an_unknown_supplier_is_unreadable():
    import io
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i, text in enumerate(["Acme Unknown Trading", "Invoice 77", "Total 100.00", "Thank you for your order"]):
        c.drawString(50, 780 - 20 * i, text)
    c.save()
    got = extract_invoice(buf.getvalue())
    assert not got.readable and "template" in got.reason


def test_garbage_bytes_are_unreadable():
    assert not extract_invoice(b"this is not a pdf").readable


@pytest.mark.parametrize("raw,want", [("$1,234.50", "1234.50"), ("52.80", "52.80"), ("12", "12"),
                                      ("abc", None), ("", None), (None, None), ("1.2.3", None)])
def test_parse_decimal(raw, want):
    assert parse_decimal(raw) == (Decimal(want) if want else None)


def test_locate_prefers_the_labelled_line():
    pages = ["Qty 40 at 52.80\nTotal 52.80\n"]
    assert locate(pages, "52.80", hints=("Total",))[1] == 2
    assert locate(pages, "99.99") is None
