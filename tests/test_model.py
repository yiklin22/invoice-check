"""The model fallback for invoices no template can read, tested with a stand-in client.
No test calls a real model."""
import io
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import select

from conftest import ACC, fixture_files
from invoicecheck import config, model, service
from invoicecheck.extraction import extract_invoice
from invoicecheck.models import Invoice, UsageCounter

PRINTED = ["Acme Unknown Trading", "Tax invoice INV-77", "Date: 03/11/2026", "PO: PO-9999",
           "WID-1 Widget 4 12.50 50.00", "Subtotal 50.00", "GST 5.00", "Total $55.00"]
PAGES = ["\n".join(PRINTED)]
GOOD = model.Reading(
    is_invoice=True, supplier_name="Acme Unknown Trading", invoice_number="INV-77", invoice_date="03/11/2026",
    po_number="PO-9999", subtotal="50.00", tax="5.00", total="$55.00",
    lines=[model.Line(sku="WID-1", description="Widget", quantity="4", unit_price="12.50", line_total="50.00")])


def pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i, text in enumerate(PRINTED):
        c.drawString(50, 780 - 20 * i, text)
    c.save()
    return buf.getvalue()


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request and returns a fixed reading."""

    def __init__(self, reading=GOOD, stop_reason="end_turn", error=None):
        self.reading, self.stop_reason, self.error, self.calls = reading, stop_reason, error, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(parsed_output=self.reading, stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=900, output_tokens=400))


def test_a_correct_reading_keeps_printed_values_as_decimals_with_evidence():
    got = model.to_invoice(PAGES, GOOD, "model:test")
    assert got.readable and got.method == "model:test" and got.invoice_number == "INV-77"
    assert got.invoice_date == date(2026, 11, 3) and got.po_number == "PO-9999"
    assert (got.subtotal, got.tax, got.total) == (Decimal("50.00"), Decimal("5.00"), Decimal("55.00"))
    assert got.lines[0].quantity == Decimal("4") and got.lines[0].line_total == Decimal("50.00")
    assert all(e.located and e.page == 1 for e in got.evidence)
    assert {e.field: e.line_text for e in got.evidence}["total"] == "Total $55.00"


def test_values_that_are_not_printed_are_dropped_or_flagged():
    invented = GOOD.model_copy(update={"total": "999.00", "po_number": "PO-1001", "lines": [
        *GOOD.lines, model.Line(sku="ZZZ-9", description="Ghost", quantity="1", unit_price="5.00", line_total="5.00")]})
    got = model.to_invoice(PAGES, invented, "model:test")
    assert got.readable and got.total is None and got.po_number is None      # header values not printed: gone
    located = {(e.field, e.line_no): e.located for e in got.evidence}
    assert not located[("total", None)] and located[("line", 1)] and not located[("line", 2)]
    assert len(got.lines) == 2                                               # the line stays, flagged, not hidden


def test_not_an_invoice_or_nothing_printed_is_unreadable():
    empty = model.Reading(is_invoice=False, supplier_name="", invoice_number="", invoice_date="", po_number="",
                          subtotal="", tax="", total="", lines=[])
    assert not model.to_invoice(PAGES, empty, "model:test").readable
    made_up = GOOD.model_copy(update={"invoice_number": "INV-1", "total": "1.00"})
    got = model.to_invoice(PAGES, made_up, "model:test")
    assert not got.readable and "not printed" in got.reason


def test_printed_dates_are_read_day_first_or_not_at_all():
    assert model.parse_printed_date("03/11/2026") == date(2026, 11, 3)
    assert model.parse_printed_date("3 November 2026") == model.parse_printed_date("2026-11-03") == date(2026, 11, 3)
    assert model.parse_printed_date("next Tuesday") is None and model.parse_printed_date(None) is None


def test_the_request_carries_the_page_text_as_data():
    client = FakeClient()
    got = model.ModelReader(client=client, model="some-model").read(PAGES)
    call = client.calls[0]
    assert got.readable and got.method == "model:some-model" and call["output_format"] is model.Reading
    assert call["messages"] == [{"role": "user", "content": f'<page number="1">\n{PAGES[0]}\n</page>'}]


@pytest.mark.parametrize("client", [
    FakeClient(stop_reason="refusal"), FakeClient(stop_reason="max_tokens"),
    FakeClient(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))),
    FakeClient(error=ValueError("output did not match the schema")),
], ids=["refusal", "max_tokens", "connection", "bad output"])
def test_a_failed_model_call_leaves_the_file_unreadable_with_the_reason(client):
    got = model.ModelReader(client=client).read(PAGES)
    assert not got.readable and "model reading" in got.reason and got.page_texts == tuple(PAGES)


# --- wired into processing ------------------------------------------------------

def calls_today(session):
    return session.scalar(select(UsageCounter.count).where(UsageCounter.account_id == ACC,
                                                           UsageCounter.kind == "model_calls")) or 0


def use_model(monkeypatch, client):
    monkeypatch.setattr(config, "MODEL_EXTRACTION", True)
    real = model.ModelReader
    monkeypatch.setattr(model, "ModelReader", lambda: real(client=client))


def process(session, files):
    service.create_batch(session, ACC, files)
    service.process_queued(session)
    return session.scalars(select(Invoice).order_by(Invoice.id)).all()


def test_the_text_layer_of_the_test_pdf_is_what_the_model_would_see():
    got = extract_invoice(pdf(), model_reader=lambda pages: model.to_invoice(pages, GOOD, "model:test"))
    assert got.readable and got.total == Decimal("55.00") and all(e.located for e in got.evidence)


def test_the_model_is_off_unless_switched_on(session, monkeypatch):
    monkeypatch.setattr(model, "ModelReader", lambda: pytest.fail("the model was used"))
    inv, = process(session, [("acme.pdf", pdf())])
    assert inv.status == "unreadable" and "template" in inv.unreadable_reason and calls_today(session) == 0


def test_a_model_read_invoice_is_never_clean_without_a_person(session, monkeypatch):
    client = FakeClient()
    use_model(monkeypatch, client)
    inv, = process(session, [("acme.pdf", pdf())])
    assert len(client.calls) == 1 and calls_today(session) == 1
    assert inv.extraction_method == f"model:{config.MODEL}" and inv.total == Decimal("55.00")
    assert inv.status == "needs_review" and "MODEL_READ" in {d.code for d in inv.discrepancies}
    with pytest.raises(service.ReviewError):
        service.decide_invoice(session, ACC, inv.id, "approve", "ana")
    assert service.usage(session, ACC)["model_calls_today"] == 1


def test_an_invoice_a_template_can_read_never_goes_to_the_model(session, monkeypatch):
    client = FakeClient()
    use_model(monkeypatch, client)
    inv, = process(session, fixture_files()[:1])
    assert inv.extraction_method.startswith("template:") and client.calls == [] and calls_today(session) == 0
    assert "MODEL_READ" not in {d.code for d in inv.discrepancies}


def test_a_failed_call_and_the_daily_cap_both_leave_the_file_unreadable(session, monkeypatch):
    client = FakeClient(stop_reason="refusal")
    use_model(monkeypatch, client)
    monkeypatch.setattr(config, "MAX_MODEL_CALLS_PER_DAY", 1)
    first, = process(session, [("acme.pdf", pdf())])
    assert first.status == "unreadable" and "model reading stopped early" in first.unreadable_reason
    second = process(session, [("acme-copy.pdf", pdf() + b"\n%copy")])[-1]
    assert second.status == "unreadable" and "template" in second.unreadable_reason
    assert len(client.calls) == 1 and calls_today(session) == 1
