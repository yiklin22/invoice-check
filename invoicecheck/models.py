"""Database tables. Money and quantities are NUMERIC and map to Decimal."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary,
                        Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(14, 2)
PRICE = Numeric(14, 4)
QTY = Numeric(14, 3)

# Invoice.status values
QUEUED, UNREADABLE, NEEDS_REVIEW, CLEAN, APPROVED, REJECTED = (
    "queued", "unreadable", "needs_review", "clean", "approved", "rejected")
# Discrepancy.state values
OPEN, ACCEPTED, DISPUTED = "open", "accepted", "disputed"


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(64), unique=True)
    supplier_name: Mapped[str] = mapped_column(String(200))
    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="PurchaseOrderLine.line_no")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (UniqueConstraint("po_id", "line_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit_price: Mapped[Decimal] = mapped_column(PRICE)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class Invoice(Base):
    """One uploaded file. The SHA-256 of the bytes is unique, so the same
    file uploaded twice is one record."""
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(300))
    pdf: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(20), default=QUEUED, index=True)
    unreadable_reason: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(String(100))
    page_texts: Mapped[list[str]] = mapped_column(JSON, default=list)
    has_manual_edits: Mapped[bool] = mapped_column(Boolean, default=False)

    supplier_name: Mapped[str | None] = mapped_column(String(200))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    po_number: Mapped[str | None] = mapped_column(String(64))
    subtotal: Mapped[Decimal | None] = mapped_column(MONEY)
    tax: Mapped[Decimal | None] = mapped_column(MONEY)
    total: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.line_no")
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="Evidence.id")
    discrepancies: Mapped[list[Discrepancy]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="Discrepancy.id")
    decisions: Mapped[list[ReviewDecision]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="ReviewDecision.id")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (UniqueConstraint("invoice_id", "line_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(300))
    quantity: Mapped[Decimal | None] = mapped_column(QTY)
    unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    line_total: Mapped[Decimal | None] = mapped_column(MONEY)
    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Evidence(Base):
    """Where a value came from: the page, the line of text, and how it was
    read. `located` is False when the value could not be found on a page."""
    __tablename__ = "extraction_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    line_no: Mapped[int | None] = mapped_column(Integer)   # invoice line, or NULL for a header field
    field: Mapped[str] = mapped_column(String(40))
    raw_text: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    page_line: Mapped[int | None] = mapped_column(Integer)
    line_text: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(100))        # "template:<issuer>" or "manual:<reviewer>"
    located: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice: Mapped[Invoice] = relationship(back_populates="evidence")


class Discrepancy(Base):
    """A finding from matching. The fingerprint is stable across re-runs so a
    reviewer's decision survives when matching is recomputed."""
    __tablename__ = "discrepancies"
    __table_args__ = (UniqueConstraint("invoice_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(10))
    line_no: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[Decimal | None] = mapped_column(MONEY)   # money at stake, when it can be computed
    state: Mapped[str] = mapped_column(String(10), default=OPEN)
    invoice: Mapped[Invoice] = relationship(back_populates="discrepancies")


class ReviewDecision(Base):
    """Append-only log of what a reviewer did."""
    __tablename__ = "reviewer_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(30))
    fingerprint: Mapped[str | None] = mapped_column(String(200))
    field: Mapped[str | None] = mapped_column(String(60))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    invoice: Mapped[Invoice] = relationship(back_populates="decisions")


class UploadBatch(Base):
    __tablename__ = "upload_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    items: Mapped[list[UploadItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="UploadItem.id")


class UploadItem(Base):
    __tablename__ = "upload_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("upload_batches.id", ondelete="CASCADE"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(300))
    outcome: Mapped[str] = mapped_column(String(30))        # new | already_uploaded | rejected_upload
    detail: Mapped[str | None] = mapped_column(Text)
    batch: Mapped[UploadBatch] = relationship(back_populates="items")
    invoice: Mapped[Invoice | None] = relationship()
