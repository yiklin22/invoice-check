"""Matching rules, tested without a database."""
from datetime import date
from decimal import Decimal as D

from invoicecheck.matching import match_all, money, show
from invoicecheck.schemas import PO, ExtractedLine, MatchInvoice, POLine

POS = {"PO-1": PO(po_number="PO-1", supplier_name="Acme Pty Ltd", lines=(
    POLine(line_no=1, sku="A-1", description="Widget", quantity=D("10"), unit_price=D("2.50")),
    POLine(line_no=2, sku="B-2", description="Gadget", quantity=D("4"), unit_price=D("10.00"))))}


def line(n=1, sku="A-1", qty="10", unit="2.50", total=None, desc="Widget"):
    total = D(total) if total else (D(qty) * D(unit)).quantize(D("0.01"))
    return ExtractedLine(line_no=n, sku=sku, description=desc, quantity=D(qty), unit_price=D(unit), line_total=total)


def inv(id=1, number="INV-1", lines=None, po="PO-1", day=date(2026, 8, 1), supplier="Acme Pty Ltd", **kw):
    lines = tuple(lines if lines is not None else [line()])
    sub = sum((ln.line_total for ln in lines), D("0"))
    tax = (sub * D("0.10")).quantize(D("0.01"))
    base = dict(id=id, supplier_name=supplier, invoice_number=number, invoice_date=day, po_number=po,
                subtotal=sub, tax=tax, total=sub + tax, lines=lines, label=f"file{id}.pdf")
    return MatchInvoice(**{**base, **kw})


def codes(invoices, pos=POS):
    return {i: sorted(f.code for f in fs) for i, fs in match_all(invoices, pos).items()}


def test_exact_match_is_clean():
    assert codes([inv()]) == {1: []}


def test_price_above_and_below_are_both_reported_with_money_impact():
    found = match_all([inv(lines=[line(unit="2.75"), line(2, "B-2", "4", "9.00", desc="Gadget")])], POS)[1]
    above, below = found
    assert (above.code, above.severity, above.impact) == ("PRICE_MISMATCH", "high", D("2.50"))
    assert (below.code, below.severity, below.impact) == ("PRICE_MISMATCH", "low", D("-4.00"))


def test_quantity_is_cumulative_in_date_order_not_upload_order():
    late = inv(1, "INV-9", [line(qty="5")], day=date(2026, 8, 20))
    early = inv(2, "INV-2", [line(qty="6")], day=date(2026, 8, 2))
    found = match_all([late, early], POS)
    assert found[2] == []
    (f,) = found[1]
    assert f.code == "QTY_EXCEEDS_PO" and f.expected == "at most 4" and f.impact == D("2.50")


def test_rejected_invoice_does_not_use_up_order_quantity():
    first = inv(1, "INV-1", [line(qty="10")], rejected=True)
    second = inv(2, "INV-2", [line(qty="10")], day=date(2026, 8, 3))
    assert codes([first, second])[2] == []


def test_same_number_twice_flags_the_later_upload_only():
    assert codes([inv(1), inv(2)]) == {1: [], 2: ["DUPLICATE_INVOICE_NUMBER"]}


def test_same_content_under_a_new_number_is_a_possible_duplicate():
    got = codes([inv(1, "INV-1"), inv(2, "INV-2", day=date(2026, 8, 9))])
    assert got == {1: [], 2: ["POSSIBLE_DUPLICATE", "QTY_EXCEEDS_PO"]}


def test_near_po_number_is_ambiguous_and_lines_are_not_matched():
    (f,) = match_all([inv(po="po 1", lines=[line(unit="9.99")])], POS)[1]
    assert f.code == "AMBIGUOUS_PO" and f.expected == "PO-1"


def test_missing_and_unknown_po():
    assert codes([inv(po=None)]) == {1: ["PO_MISSING"]}
    assert codes([inv(po="PO-404")]) == {1: ["PO_NOT_FOUND"]}


def test_sku_not_ordered_and_sku_matched_only_by_description():
    assert codes([inv(lines=[line(sku="Z-9", desc="Sprocket")])]) == {1: ["SKU_NOT_ON_PO"]}
    assert codes([inv(lines=[line(sku="A-01", desc="widget")])]) == {1: ["AMBIGUOUS_SKU"]}


def test_sku_on_two_order_lines_is_ambiguous():
    pos = {"PO-1": PO(po_number="PO-1", supplier_name="Acme Pty Ltd", lines=(
        POLine(line_no=1, sku="A-1", description="Widget", quantity=D("5"), unit_price=D("2.50")),
        POLine(line_no=2, sku="A-1", description="Widget, promo", quantity=D("5"), unit_price=D("2.00"))))}
    assert codes([inv()], pos) == {1: ["AMBIGUOUS_SKU"]}


def test_supplier_mismatch():
    assert codes([inv(supplier="Other Co")]) == {1: ["SUPPLIER_MISMATCH"]}


def test_line_arithmetic_and_totals():
    assert codes([inv(lines=[line(total="26.00")])]) == {1: ["LINE_ARITHMETIC"]}
    assert codes([inv(total=D("99.00"))]) == {1: ["TOTAL_MISMATCH"]}
    assert codes([inv(subtotal=D("20.00"), tax=D("2.00"), total=D("22.00"))]) == {1: ["TOTAL_MISMATCH"]}


def test_missing_fields_are_reported_not_ignored():
    got = match_all([inv(invoice_date=None, total=None, lines=[])], POS)[1]
    assert sorted(f.key for f in got if f.code == "MISSING_FIELD") == ["invoice_date", "lines", "total"]


def test_unreadable_gets_exactly_one_finding():
    bad = MatchInvoice(id=1, readable=False, unreadable_reason="no text layer")
    (f,) = match_all([bad], POS)[1]
    assert f.code == "UNREADABLE" and f.message == "no text layer"


def test_cent_rounding_is_half_up_on_decimals():
    # 3 x 0.335 = 1.005 -> 1.01 (binary floats would give 1.00)
    assert codes([inv(lines=[line(qty="3", unit="0.335", total="1.01")], po=None)]) == {1: ["PO_MISSING"]}


def test_show_and_money():
    assert [show(D(x)) for x in ("4", "4.000", "2.5", "0.335", "1E+1")] == ["4", "4", "2.5", "0.335", "10"]
    assert [money(D(x)) for x in ("25.0000", "2.5", "0.335", "26")] == ["25.00", "2.50", "0.335", "26.00"]
