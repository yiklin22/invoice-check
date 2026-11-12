"""End to end on PostgreSQL: the seven acceptance claims and the review rules."""
import csv
import io
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from conftest import FIXTURES, fixture_files
from invoicecheck import service
from invoicecheck.models import (APPROVED, CLEAN, NEEDS_REVIEW, REJECTED, UNREADABLE, Discrepancy,
                                 Invoice, InvoiceLine, PurchaseOrderLine)


def by_file(session, name) -> Invoice:
    return session.scalar(select(Invoice).where(Invoice.filename == name))


def test_every_seeded_discrepancy_is_surfaced_and_nothing_else(loaded, manifest):
    for entry in manifest:
        inv = by_file(loaded, entry["file"])
        assert sorted({d.code for d in inv.discrepancies}) == entry["expected_codes"], entry["file"]
        assert inv.status == entry["expected_status"], entry["file"]


def test_unreadable_inputs_never_appear_clean(loaded, manifest):
    for entry in (m for m in manifest if not m["readable"]):
        inv = by_file(loaded, entry["file"])
        assert inv.status == UNREADABLE and inv.unreadable_reason
        assert inv.total is None and not inv.lines
        with pytest.raises(service.ReviewError):
            service.decide_invoice(loaded, inv.id, "approve", "tester")


def test_reupload_creates_no_duplicate_records(loaded):
    counts = lambda: [loaded.scalar(select(func.count()).select_from(t))
                      for t in (Invoice, InvoiceLine, Discrepancy)]
    before = counts()
    batch = service.create_batch(loaded, fixture_files())
    assert {i.outcome for i in batch.items} == {"already_uploaded"}
    assert service.process_queued(loaded) == 0
    assert counts() == before


def test_reimporting_orders_creates_no_duplicate_lines(loaded):
    before = loaded.scalar(select(func.count()).select_from(PurchaseOrderLine))
    assert service.import_purchase_orders(loaded, (FIXTURES / "purchase_orders.csv").read_text())["ok"]
    assert loaded.scalar(select(func.count()).select_from(PurchaseOrderLine)) == before


def test_result_does_not_depend_on_upload_order(session, manifest):
    service.import_purchase_orders(session, (FIXTURES / "purchase_orders.csv").read_text())
    files = fixture_files()
    # Reverse order, one file per upload, except keep each original before its same-number copy.
    files.sort(key=lambda f: (f[0].startswith("09_"), -int(f[0][:2])))
    for f in files:
        service.create_batch(session, [f])
        service.process_queued(session)
    for entry in manifest:
        inv = by_file(session, entry["file"])
        assert sorted({d.code for d in inv.discrepancies}) == entry["expected_codes"], entry["file"]


def test_money_is_stored_as_decimal(loaded):
    inv = by_file(loaded, "01_clean_northwind.pdf")
    assert isinstance(inv.total, Decimal) and inv.total == Decimal("2669.70")
    assert isinstance(inv.lines[0].unit_price, Decimal)


def test_bad_order_file_is_rejected_whole(session):
    text = "po_number,supplier,line_no,sku,description,quantity,unit_price\nPO-1,Acme,1,A,Thing,5,2.50\nPO-2,Acme,1,B,Thing,many,1\n"
    result = service.import_purchase_orders(session, text)
    assert not result["ok"] and "Row 3" in result["errors"][0]
    assert session.scalar(select(func.count()).select_from(PurchaseOrderLine)) == 0
    assert not service.import_purchase_orders(session, "a,b\n1,2\n")["ok"]


def test_correcting_the_po_number_runs_full_matching(loaded):
    inv = by_file(loaded, "08_ambiguous_po_format.pdf")
    service.apply_edit(loaded, inv.id, "ana", {"po_number": "PO-1007"}, [])
    loaded.refresh(inv)
    assert inv.discrepancies == [] and inv.status == NEEDS_REVIEW     # edited, so a person must approve
    assert [(d.action, d.field, d.old_value, d.new_value) for d in inv.decisions] == \
        [("edit_field", "po_number", "PO 1007", "PO-1007")]
    assert any(e.field == "po_number" and e.method == "manual:ana" for e in inv.evidence)
    service.decide_invoice(loaded, inv.id, "approve", "ana")
    assert inv.status == APPROVED


def test_approval_needs_every_discrepancy_accepted_with_a_reason(loaded):
    inv = by_file(loaded, "11_price_above_po.pdf")
    (d,) = inv.discrepancies
    with pytest.raises(service.ReviewError):
        service.decide_invoice(loaded, inv.id, "approve", "ana")
    with pytest.raises(service.ReviewError):
        service.decide_discrepancy(loaded, inv.id, d.fingerprint, "accept", "ana", note=" ")
    service.decide_discrepancy(loaded, inv.id, d.fingerprint, "dispute", "ana")
    with pytest.raises(service.ReviewError):
        service.decide_invoice(loaded, inv.id, "approve", "ana")
    service.decide_discrepancy(loaded, inv.id, d.fingerprint, "accept", "ana", note="Price rise agreed by email")
    service.decide_invoice(loaded, inv.id, "approve", "ana")
    assert inv.status == APPROVED


def test_edit_after_approval_invalidates_it(loaded):
    inv = by_file(loaded, "01_clean_northwind.pdf")
    service.decide_invoice(loaded, inv.id, "approve", "ana")
    service.apply_edit(loaded, inv.id, "ben", {}, [{"line_no": "1", "quantity": "41"}])
    loaded.refresh(inv)
    assert inv.status == NEEDS_REVIEW
    assert "approval_invalidated" in [d.action for d in inv.decisions]
    assert {d.code for d in inv.discrepancies} == {"LINE_ARITHMETIC", "QTY_EXCEEDS_PO"}


def test_changed_facts_reopen_an_accepted_discrepancy(loaded):
    inv = by_file(loaded, "11_price_above_po.pdf")
    (d,) = inv.discrepancies
    service.decide_discrepancy(loaded, inv.id, d.fingerprint, "accept", "ana", note="agreed")
    service.apply_edit(loaded, inv.id, "ana", {}, [{"line_no": "1", "unit_price": "25.00", "line_total": "300.00"}])
    loaded.refresh(inv)
    price = next(x for x in inv.discrepancies if x.code == "PRICE_MISMATCH")
    assert price.state == "open" and price.actual == "25.00"


def test_rejecting_an_invoice_frees_its_order_quantity(loaded):
    part2 = by_file(loaded, "07_clean_kestrel_part2.pdf")
    extra = by_file(loaded, "15_quantity_cumulative.pdf")
    assert [d.code for d in extra.discrepancies] == ["QTY_EXCEEDS_PO"]
    service.decide_invoice(loaded, part2.id, "reject", "ana", note="Goods returned")
    loaded.refresh(extra)
    # 6 of 12 units are now invoiced, so 4 more fit the order
    assert extra.discrepancies == [] and extra.status == CLEAN
    assert part2.status == REJECTED
    service.decide_invoice(loaded, part2.id, "reopen", "ana")
    loaded.refresh(extra)
    assert [d.code for d in extra.discrepancies] == ["QTY_EXCEEDS_PO"] and extra.status == NEEDS_REVIEW


def test_unreadable_invoice_can_be_keyed_in_but_is_never_auto_clean(loaded):
    inv = by_file(loaded, "18_unreadable_scan.pdf")
    service.apply_edit(loaded, inv.id, "ana", {
        "supplier_name": "Northwind Paper Co", "invoice_number": "NP-10480", "invoice_date": "2026-08-21",
        "po_number": "PO-1013", "subtotal": "898.00", "tax": "89.80", "total": "987.80"}, [
        {"line_no": "", "sku": "NP-A3-80", "description": "A3", "quantity": "10", "unit_price": "61.00", "line_total": "610.00"},
        {"line_no": "", "sku": "NP-PAD-A5", "description": "A5", "quantity": "20", "unit_price": "14.40", "line_total": "288.00"}])
    loaded.refresh(inv)
    assert inv.status == NEEDS_REVIEW and inv.status != CLEAN
    # invoice 20 bills the same A3 paper against PO-1013 and is dated later, so it now exceeds the order
    assert [d.code for d in inv.discrepancies] == []
    later = by_file(loaded, "20_sku_not_ordered_total_wrong.pdf")
    assert "QTY_EXCEEDS_PO" in {d.code for d in later.discrepancies}


def test_invalid_edit_changes_nothing(loaded):
    inv = by_file(loaded, "02_clean_kestrel.pdf")
    with pytest.raises(service.ReviewError):
        service.apply_edit(loaded, inv.id, "ana", {"total": "lots"}, [])
    loaded.rollback()
    loaded.refresh(inv)
    assert inv.status == CLEAN and inv.total == Decimal("1405.80") and not inv.decisions


def test_upload_limits(session, monkeypatch):
    monkeypatch.setattr(service.config, "MAX_UPLOAD_BYTES", 10)
    batch = service.create_batch(session, [("big.pdf", b"x" * 11), ("empty.pdf", b"")])
    assert [i.outcome for i in batch.items] == ["rejected_upload", "rejected_upload"]
    assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_export_lists_every_discrepancy_with_its_decision(loaded):
    inv = by_file(loaded, "14_quantity_above_po.pdf")
    service.decide_discrepancy(loaded, inv.id, inv.discrepancies[0].fingerprint, "accept", "ana", note="Extra agreed")
    rows = list(csv.DictReader(io.StringIO(service.export_discrepancies_csv(loaded))))
    assert len(rows) == loaded.scalar(select(func.count()).select_from(Discrepancy))
    row = next(r for r in rows if r["file"] == "14_quantity_above_po.pdf")
    assert (row["code"], row["decision"], row["decision_note"], row["decided_by"], row["impact"]) == \
        ("QTY_EXCEEDS_PO", "accepted", "Extra agreed", "ana", "50.70")
    summary = list(csv.DictReader(io.StringIO(service.export_invoices_csv(loaded))))
    assert len(summary) == 20 and sum(r["status"] == "clean" for r in summary) == 7


def test_delete_removes_the_file_and_its_findings(loaded):
    original = by_file(loaded, "03_clean_bluegum.pdf")
    service.delete_invoice(loaded, original.id)
    copy = by_file(loaded, "09_duplicate_same_number.pdf")
    loaded.refresh(copy)
    assert copy.discrepancies == [] and copy.status == CLEAN
