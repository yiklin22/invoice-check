"""Typed data passed between extraction, matching and the API.

These are plain values with no database session attached, so extraction
and matching can be tested without PostgreSQL.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class EvidenceItem(Model):
    field: str
    line_no: int | None = None      # invoice line number, None for header fields
    raw_text: str | None = None     # the characters as printed
    page: int | None = None         # 1-based
    page_line: int | None = None    # 1-based line within the page text
    line_text: str | None = None
    method: str
    located: bool = False


class ExtractedLine(Model):
    line_no: int
    sku: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None


class ExtractedInvoice(Model):
    """Result of reading one file. `readable=False` means nothing below can
    be trusted and `reason` says why."""
    readable: bool
    reason: str | None = None
    method: str | None = None
    page_texts: tuple[str, ...] = ()
    supplier_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    po_number: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    currency: str | None = None
    lines: tuple[ExtractedLine, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()


class POLine(Model):
    line_no: int
    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal


class PO(Model):
    po_number: str
    supplier_name: str
    lines: tuple[POLine, ...]


class MatchInvoice(Model):
    """What matching needs to know about one invoice."""
    id: int
    supplier_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    po_number: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    lines: tuple[ExtractedLine, ...] = ()
    readable: bool = True
    unreadable_reason: str | None = None
    rejected: bool = False          # rejected invoices do not use up PO quantity
    label: str = ""                 # filename, for messages


class Finding(Model):
    code: str
    severity: str                   # high | medium | low
    message: str
    line_no: int | None = None
    key: str = "-"                  # distinguishes two findings of one code on one line
    expected: str | None = None
    actual: str | None = None
    impact: Decimal | None = None

    @property
    def fingerprint(self) -> str:
        return f"{self.code}:{self.line_no if self.line_no is not None else '-'}:{self.key}"
