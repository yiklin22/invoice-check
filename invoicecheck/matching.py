"""Compare invoices with purchase orders. Pure functions, no database.

Rules, in order:
1. A file that could not be read gets UNREADABLE and nothing else.
2. Purchase orders and SKUs are matched exactly. A near match (for example
   "PO 1007" for "PO-1007") is reported as AMBIGUOUS and left for a person;
   it is never used to pass an invoice.
3. Quantities are cumulative per PO line across invoices, in invoice-date
   order, so the result does not depend on upload order.
All arithmetic is Decimal.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .schemas import PO, Finding, MatchInvoice

CENT = Decimal("0.01")
REQUIRED = ("supplier_name", "invoice_number", "invoice_date", "total")


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def show(x: Decimal | None, places: int = 0) -> str:
    """Decimal as a person would write it: no exponent, no trailing zeros
    beyond `places` decimals."""
    if x is None:
        return ""
    whole, _, frac = f"{x:f}".partition(".")
    frac = frac.rstrip("0").ljust(places, "0")
    return f"{whole}.{frac}" if frac else whole


def money(x: Decimal | None) -> str:
    return show(x, 2)


def order_key(inv: MatchInvoice):
    return (inv.invoice_date or date.max, inv.invoice_number or "", inv.id)


def _field_findings(inv: MatchInvoice) -> list[Finding]:
    out = []
    for name in REQUIRED:
        if getattr(inv, name) in (None, ""):
            out.append(Finding(code="MISSING_FIELD", severity="high", key=name,
                               message=f"Required field {name} is missing."))
    if not inv.lines:
        out.append(Finding(code="MISSING_FIELD", severity="high", key="lines",
                           message="No line items were read from this invoice."))
    for ln in inv.lines:
        for name in ("sku", "quantity", "unit_price", "line_total"):
            if getattr(ln, name) in (None, ""):
                out.append(Finding(code="MISSING_FIELD", severity="high", line_no=ln.line_no, key=name,
                                   message=f"Line {ln.line_no}: {name} is missing."))
    return out


def _arithmetic_findings(inv: MatchInvoice) -> list[Finding]:
    out = []
    for ln in inv.lines:
        if None in (ln.quantity, ln.unit_price, ln.line_total):
            continue
        computed = cents(ln.quantity * ln.unit_price)
        if computed != ln.line_total:
            out.append(Finding(
                code="LINE_ARITHMETIC", severity="high", line_no=ln.line_no,
                message=f"Line {ln.line_no}: {show(ln.quantity)} x {money(ln.unit_price)} is "
                        f"{money(computed)}, but the invoice says {money(ln.line_total)}.",
                expected=money(computed), actual=money(ln.line_total), impact=ln.line_total - computed))
    totals = [ln.line_total for ln in inv.lines]
    if inv.subtotal is not None and totals and None not in totals:
        line_sum = sum(totals, Decimal("0"))
        if line_sum != inv.subtotal:
            out.append(Finding(
                code="TOTAL_MISMATCH", severity="high", key="subtotal",
                message=f"Lines add up to {money(line_sum)}, but the subtotal says {money(inv.subtotal)}.",
                expected=money(line_sum), actual=money(inv.subtotal), impact=inv.subtotal - line_sum))
    if None not in (inv.subtotal, inv.tax, inv.total):
        computed = inv.subtotal + inv.tax
        if computed != inv.total:
            out.append(Finding(
                code="TOTAL_MISMATCH", severity="high", key="total",
                message=f"Subtotal plus tax is {money(computed)}, but the total says {money(inv.total)}.",
                expected=money(computed), actual=money(inv.total), impact=inv.total - computed))
    return out


def _resolve_po(inv: MatchInvoice, pos: dict[str, PO]) -> tuple[PO | None, list[Finding]]:
    if not inv.po_number:
        return None, [Finding(code="PO_MISSING", severity="high",
                              message="The invoice does not state a purchase order number.")]
    if inv.po_number in pos:
        return pos[inv.po_number], []
    near = sorted(p for p in pos if norm(p) == norm(inv.po_number))
    if near:
        return None, [Finding(
            code="AMBIGUOUS_PO", severity="medium",
            message=f'The invoice says "{inv.po_number}". No order has exactly that number; '
                    f"{' or '.join(near)} is close. Confirm and correct the PO number to match lines.",
            expected=" or ".join(near), actual=inv.po_number)]
    return None, [Finding(code="PO_NOT_FOUND", severity="high",
                          message=f"Purchase order {inv.po_number} is not in the order list.",
                          actual=inv.po_number)]


def match_all(invoices: list[MatchInvoice], pos: dict[str, PO]) -> dict[int, list[Finding]]:
    """Return the findings for every invoice, keyed by invoice id."""
    out: dict[int, list[Finding]] = {inv.id: [] for inv in invoices}
    readable = [inv for inv in invoices if inv.readable]
    for inv in invoices:
        if not inv.readable:
            out[inv.id].append(Finding(code="UNREADABLE", severity="high",
                                       message=inv.unreadable_reason or "The file could not be read."))

    # Same supplier and invoice number: the first one uploaded is the original.
    by_number: dict[tuple[str, str], list[MatchInvoice]] = defaultdict(list)
    for inv in sorted(readable, key=lambda i: i.id):
        if inv.invoice_number and inv.supplier_name and not inv.rejected:
            by_number[(norm(inv.supplier_name), norm(inv.invoice_number))].append(inv)
    repeats: set[int] = set()
    for group in by_number.values():
        for later in group[1:]:
            repeats.add(later.id)
            out[later.id].append(Finding(
                code="DUPLICATE_INVOICE_NUMBER", severity="high",
                message=f"Invoice number {later.invoice_number} from {later.supplier_name} "
                        f"was already received ({group[0].label or f'invoice {group[0].id}'}).",
                expected="one invoice per number", actual=later.invoice_number, impact=later.total))

    used: dict[tuple[str, str], Decimal] = defaultdict(Decimal)      # (po, sku) -> quantity invoiced so far
    seen: dict[tuple, MatchInvoice] = {}                              # content signature -> first invoice
    for inv in sorted(readable, key=order_key):
        findings = out[inv.id]
        findings += _field_findings(inv)
        findings += _arithmetic_findings(inv)
        po, po_findings = _resolve_po(inv, pos)
        findings += po_findings
        counts = inv.id not in repeats and not inv.rejected

        if counts and inv.po_number and inv.total is not None and inv.lines:
            signature = (norm(inv.supplier_name), inv.po_number, inv.total,
                         tuple(sorted((ln.sku or "", ln.quantity, ln.unit_price) for ln in inv.lines)))
            first = seen.setdefault(signature, inv)
            if first is not inv:
                findings.append(Finding(
                    code="POSSIBLE_DUPLICATE", severity="high",
                    message=f"Same supplier, order, lines and total as invoice {first.invoice_number} "
                            f"({first.label or f'invoice {first.id}'}), under a different invoice number.",
                    expected=first.invoice_number, actual=inv.invoice_number, impact=inv.total))

        if po is None:
            continue
        if norm(po.supplier_name) != norm(inv.supplier_name):
            findings.append(Finding(
                code="SUPPLIER_MISMATCH", severity="high",
                message=f"{po.po_number} was placed with {po.supplier_name}, "
                        f"but this invoice is from {inv.supplier_name}.",
                expected=po.supplier_name, actual=inv.supplier_name))
        for ln in inv.lines:
            if not ln.sku:
                continue
            same_sku = [p for p in po.lines if p.sku == ln.sku]
            if len(same_sku) != 1:
                by_text = [p for p in po.lines if norm(p.description) == norm(ln.description)]
                if len(same_sku) > 1 or (not same_sku and len(by_text) == 1):
                    options = same_sku or by_text
                    findings.append(Finding(
                        code="AMBIGUOUS_SKU", severity="medium", line_no=ln.line_no,
                        message=f"Line {ln.line_no}: {ln.sku} does not identify one line of {po.po_number}. "
                                f"Candidates: {', '.join(f'line {p.line_no} ({p.sku})' for p in options)}.",
                        expected=", ".join(p.sku for p in options), actual=ln.sku))
                else:
                    findings.append(Finding(
                        code="SKU_NOT_ON_PO", severity="high", line_no=ln.line_no,
                        message=f"Line {ln.line_no}: {ln.sku} was not ordered on {po.po_number}.",
                        actual=ln.sku, impact=ln.line_total))
                continue
            ordered = same_sku[0]
            if ln.unit_price is not None and ln.unit_price != ordered.unit_price:
                above = ln.unit_price > ordered.unit_price
                diff = ln.unit_price - ordered.unit_price
                findings.append(Finding(
                    code="PRICE_MISMATCH", severity="high" if above else "low", line_no=ln.line_no,
                    message=f"Line {ln.line_no}: {ln.sku} is billed at {money(ln.unit_price)}, "
                            f"{'above' if above else 'below'} the order price {money(ordered.unit_price)}.",
                    expected=money(ordered.unit_price), actual=money(ln.unit_price),
                    impact=cents(diff * ln.quantity) if ln.quantity is not None else None))
            if ln.quantity is None or inv.id in repeats:
                continue
            before = used[(po.po_number, ln.sku)]
            if before + ln.quantity > ordered.quantity:
                remaining = max(ordered.quantity - before, Decimal("0"))
                excess = ln.quantity - remaining
                findings.append(Finding(
                    code="QTY_EXCEEDS_PO", severity="high", line_no=ln.line_no,
                    message=f"Line {ln.line_no}: {show(ln.quantity)} x {ln.sku} billed, but only "
                            f"{show(remaining)} remain on {po.po_number} "
                            f"(ordered {show(ordered.quantity)}, already invoiced {show(before)}).",
                    expected=f"at most {show(remaining)}", actual=show(ln.quantity),
                    impact=cents(excess * ordered.unit_price)))
            if counts:
                used[(po.po_number, ln.sku)] = before + ln.quantity
    return out
