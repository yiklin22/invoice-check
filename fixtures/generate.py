"""Generate the synthetic fixture set.

Writes 20 invoice PDFs, one purchase-order CSV and a manifest holding the
ground truth (expected fields and expected discrepancy codes) for each file.
Everything here is invented: the buyer, the suppliers, the orders.

Run:  uv run python fixtures/generate.py
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP
from decimal import Decimal as D
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent
CURRENCY = "AUD"
GST = D("0.10")
BUYER = "Harbourline Facilities Pty Ltd"
SUPPLIERS = {
    "NW": "Northwind Paper Co",
    "KF": "Kestrel Fasteners Pty Ltd",
    "BG": "Bluegum Cleaning Supplies",
}

# po_number -> (supplier key, [(sku, description, quantity, unit_price)])
POS: dict[str, tuple[str, list[tuple[str, str, int, str]]]] = {
    "PO-1001": ("NW", [("NP-A4-80", "A4 copy paper 80gsm (carton)", 40, "52.80"),
                       ("NP-ENV-DL", "DL envelopes (box 500)", 10, "31.50")]),
    "PO-1002": ("NW", [("NP-A3-80", "A3 copy paper 80gsm (carton)", 20, "61.00"),
                       ("NP-LBL-24", "Labels 24-up (pack 100)", 15, "18.25")]),
    "PO-1003": ("KF", [("KF-M8-50", "M8x50 hex bolt zinc (box 100)", 30, "24.40"),
                       ("KF-M8-NUT", "M8 hex nut zinc (box 200)", 30, "9.90"),
                       ("KF-WSH-8", "M8 flat washer (box 500)", 60, "4.15")]),
    "PO-1004": ("KF", [("KF-M10-75", "M10x75 hex bolt zinc (box 50)", 12, "41.20"),
                       ("KF-ANC-10", "M10 sleeve anchor (box 25)", 8, "36.75")]),
    "PO-1005": ("BG", [("BG-DET-5L", "Neutral detergent 5L", 24, "17.60"),
                       ("BG-MOP-HD", "Mop head heavy duty", 36, "8.45")]),
    "PO-1006": ("BG", [("BG-GLV-M", "Nitrile gloves M (box 100)", 50, "12.30"),
                       ("BG-BIN-80", "Bin liners 80L (roll 50)", 40, "9.75"),
                       ("BG-SPR-750", "Spray bottle 750ml", 60, "3.20")]),
    "PO-1007": ("NW", [("NP-A4-80", "A4 copy paper 80gsm (carton)", 25, "52.80"),
                       ("NP-PAD-A5", "A5 ruled notepad (pack 10)", 30, "14.40")]),
    "PO-1008": ("BG", [("BG-DET-5L", "Neutral detergent 5L", 12, "17.60"),
                       ("BG-GLV-M", "Nitrile gloves M (box 100)", 20, "12.30")]),
    "PO-1009": ("NW", [("NP-LBL-24", "Labels 24-up (pack 100)", 40, "18.25"),
                       ("NP-ENV-DL", "DL envelopes (box 500)", 20, "31.50")]),
    "PO-1010": ("KF", [("KF-M8-50", "M8x50 hex bolt zinc (box 100)", 18, "24.40"),
                       ("KF-WSH-8", "M8 flat washer (box 500)", 40, "4.15")]),
    "PO-1011": ("BG", [("BG-MOP-HD", "Mop head heavy duty", 24, "8.45"),
                       ("BG-BIN-80", "Bin liners 80L (roll 50)", 30, "9.75")]),
    "PO-1012": ("BG", [("BG-SPR-750", "Spray bottle 750ml", 48, "3.20"),
                       ("BG-DET-5L", "Neutral detergent 5L", 10, "17.60")]),
    "PO-1013": ("NW", [("NP-A3-80", "A3 copy paper 80gsm (carton)", 10, "61.00"),
                       ("NP-PAD-A5", "A5 ruled notepad (pack 10)", 20, "14.40")]),
}


def money(x: D) -> D:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Line:
    sku: str
    description: str
    quantity: int
    unit_price: D
    line_total: D


def take(po: str, sku: str, qty: int | None = None, unit: str | None = None,
         total: str | None = None) -> Line:
    """Copy a PO line, optionally overriding what the supplier bills."""
    for s, desc, q, price in POS[po][1]:
        if s == sku:
            quantity = q if qty is None else qty
            unit_price = D(unit or price)
            line_total = D(total) if total else money(quantity * unit_price)
            return Line(s, desc, quantity, unit_price, line_total)
    raise KeyError((po, sku))


def full(po: str) -> list[Line]:
    return [take(po, sku) for sku, *_ in POS[po][1]]


@dataclass
class Inv:
    n: int
    slug: str
    category: str
    sup: str
    number: str
    day: date
    po: str | None
    lines: list[Line]
    expect: list[str] = field(default_factory=list)
    po_text: str | None = None       # how the PO is printed, if not verbatim
    total_override: str | None = None
    kind: str = "text"               # text | scan | corrupt
    two_page: bool = False
    stamp: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.n:02d}_{self.slug}.pdf"

    @property
    def subtotal(self) -> D:
        return sum((ln.line_total for ln in self.lines), D("0"))

    @property
    def tax(self) -> D:
        return money(self.subtotal * GST)

    @property
    def total(self) -> D:
        return D(self.total_override) if self.total_override else self.subtotal + self.tax

    @property
    def po_printed(self) -> str | None:
        return self.po_text if self.po_text is not None else self.po


INVOICES = [
    Inv(1, "clean_northwind", "clean", "NW", "NP-10432", date(2026, 8, 3), "PO-1001", full("PO-1001")),
    Inv(2, "clean_kestrel", "clean", "KF", "KF2026-0087", date(2026, 8, 5), "PO-1003", full("PO-1003")),
    Inv(3, "clean_bluegum", "clean", "BG", "BG/5521", date(2026, 8, 7), "PO-1005", full("PO-1005")),
    Inv(4, "clean_northwind", "clean", "NW", "NP-10447", date(2026, 8, 10), "PO-1002", full("PO-1002")),
    Inv(5, "clean_kestrel_part1", "clean", "KF", "KF2026-0092", date(2026, 8, 6), "PO-1004",
        [take("PO-1004", "KF-M10-75", qty=6), take("PO-1004", "KF-ANC-10")]),
    Inv(6, "clean_bluegum_two_pages", "clean", "BG", "BG/5530", date(2026, 8, 11), "PO-1006",
        full("PO-1006"), two_page=True),
    Inv(7, "clean_kestrel_part2", "clean", "KF", "KF2026-0104", date(2026, 8, 13), "PO-1004",
        [take("PO-1004", "KF-M10-75", qty=6)]),
    Inv(8, "ambiguous_po_format", "ambiguous", "NW", "NP-10461", date(2026, 8, 12), "PO-1007",
        full("PO-1007"), expect=["AMBIGUOUS_PO"], po_text="PO 1007"),
    Inv(9, "duplicate_same_number", "duplicate", "BG", "BG/5521", date(2026, 8, 7), "PO-1005",
        full("PO-1005"), expect=["DUPLICATE_INVOICE_NUMBER"], stamp="COPY"),
    Inv(10, "duplicate_rebilled", "duplicate", "KF", "KF2026-0131", date(2026, 8, 19), "PO-1003",
        full("PO-1003"), expect=["POSSIBLE_DUPLICATE", "QTY_EXCEEDS_PO"]),
    Inv(11, "price_above_po", "wrong_price", "BG", "BG/5544", date(2026, 8, 14), "PO-1008",
        [take("PO-1008", "BG-DET-5L", unit="19.10"), take("PO-1008", "BG-GLV-M")],
        expect=["PRICE_MISMATCH"]),
    Inv(12, "price_below_po", "wrong_price", "NW", "NP-10475", date(2026, 8, 14), "PO-1009",
        [take("PO-1009", "NP-LBL-24", unit="17.25"), take("PO-1009", "NP-ENV-DL")],
        expect=["PRICE_MISMATCH"]),
    Inv(13, "line_arithmetic", "wrong_price", "KF", "KF2026-0118", date(2026, 8, 17), "PO-1010",
        [take("PO-1010", "KF-M8-50", total="493.20"), take("PO-1010", "KF-WSH-8")],
        expect=["LINE_ARITHMETIC"]),
    Inv(14, "quantity_above_po", "excess_quantity", "BG", "BG/5551", date(2026, 8, 18), "PO-1011",
        [take("PO-1011", "BG-MOP-HD", qty=30), take("PO-1011", "BG-BIN-80")],
        expect=["QTY_EXCEEDS_PO"]),
    Inv(15, "quantity_cumulative", "excess_quantity", "KF", "KF2026-0140", date(2026, 8, 20), "PO-1004",
        [take("PO-1004", "KF-M10-75", qty=4)], expect=["QTY_EXCEEDS_PO"]),
    Inv(16, "order_not_found", "missing_order", "KF", "KF2026-0122", date(2026, 8, 18), "PO-1999",
        [Line("KF-M8-NUT", "M8 hex nut zinc (box 200)", 20, D("9.90"), D("198.00"))],
        expect=["PO_NOT_FOUND"]),
    Inv(17, "no_po_on_invoice", "missing_order", "BG", "BG/5560", date(2026, 8, 20), None,
        full("PO-1012"), expect=["PO_MISSING"]),
    Inv(18, "unreadable_scan", "unreadable", "NW", "NP-10480", date(2026, 8, 21), "PO-1013",
        full("PO-1013"), expect=["UNREADABLE"], kind="scan"),
    Inv(19, "unreadable_truncated", "unreadable", "BG", "BG/5571", date(2026, 8, 21), "PO-1012",
        full("PO-1012"), expect=["UNREADABLE"], kind="corrupt"),
    Inv(20, "sku_not_ordered_total_wrong", "wrong_total", "NW", "NP-10492", date(2026, 8, 24), "PO-1013",
        [take("PO-1013", "NP-A3-80"), Line("NP-TNR-BK", "Toner cartridge black", 2, D("89.00"), D("178.00"))],
        expect=["SKU_NOT_ON_PO", "TOTAL_MISMATCH"], total_override="876.80"),
]


# --- three supplier layouts -------------------------------------------------

def fmt_nw(x: D) -> str:
    return f"{x:,.2f}"


def fmt_kf(x: D) -> str:
    return f"{x:.2f}"


def fmt_bg(x: D) -> str:
    return f"${x:,.2f}"


def text_lines(inv: Inv) -> tuple[list[str], list[str], list[str]]:
    """Return (header lines, table lines, totals lines) as printed."""
    po = inv.po_printed
    if inv.sup == "NW":
        head = ["TAX INVOICE", f"Invoice No: {inv.number}",
                f"Invoice Date: {inv.day:%d/%m/%Y}"]
        if po:
            head.append(f"PO Number: {po}")
        table = [f"{'SKU':<12} {'Description':<34} {'Qty':>4} {'Unit Price':>11} {'Amount':>11}"]
        table += [f"{ln.sku:<12} {ln.description:<34} {ln.quantity:>4} {fmt_nw(ln.unit_price):>11} "
                  f"{fmt_nw(ln.line_total):>11}" for ln in inv.lines]
        totals = [f"Subtotal {fmt_nw(inv.subtotal):>14}", f"GST 10% {fmt_nw(inv.tax):>15}",
                  f"Total {CURRENCY} {fmt_nw(inv.total):>13}"]
    elif inv.sup == "KF":
        head = [f"Invoice # {inv.number}", f"Invoice date: {inv.day.isoformat()}"]
        if po:
            head.append(f"Your order ref: {po}")
        table = [f"{'Qty':>4}  {'Item code':<11} {'Description':<34} {'Each':>9} {'Line total':>10}"]
        table += [f"{ln.quantity:>4}  {ln.sku:<11} {ln.description:<34} {fmt_kf(ln.unit_price):>9} "
                  f"{fmt_kf(ln.line_total):>10}" for ln in inv.lines]
        totals = [f"Net {fmt_kf(inv.subtotal):>18}", f"GST {fmt_kf(inv.tax):>18}",
                  f"Amount due {fmt_kf(inv.total):>11}"]
    else:
        head = [f"INVOICE {inv.number}", f"Issued {inv.day.day} {inv.day:%B %Y}"]
        if po:
            head.append(f"Purchase order {po}")
        table = [f"{'Item':<11} {'Description':<34} {'Qty':>4} {'Unit':>10} {'Total':>11}"]
        table += [f"{ln.sku:<11} {ln.description:<34} {ln.quantity:>4} {fmt_bg(ln.unit_price):>10} "
                  f"{fmt_bg(ln.line_total):>11}" for ln in inv.lines]
        totals = [f"Sub-total {fmt_bg(inv.subtotal):>13}", f"GST (10%) {fmt_bg(inv.tax):>13}",
                  f"TOTAL DUE {fmt_bg(inv.total):>13}"]
    return head, table, totals


def render_text_pdf(inv: Inv) -> bytes:
    head, table, totals = text_lines(inv)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    w, h = A4
    c.setTitle(f"Invoice {inv.number}")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, h - 60, SUPPLIERS[inv.sup])
    c.setFont("Helvetica", 9)
    c.drawString(50, h - 75, "ABN 00 000 000 000 (fictional supplier, test data)")
    if inv.stamp:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(w - 130, h - 60, inv.stamp)
    y = h - 110
    c.setFont("Helvetica", 11)
    for ln in head:
        c.drawString(50, y, ln)
        y -= 16
    y -= 6
    c.drawString(50, y, f"Bill to: {BUYER}")
    y -= 30
    c.setFont("Courier-Bold", 9)
    c.drawString(50, y, table[0])
    c.setFont("Courier", 9)
    for row in table[1:]:
        y -= 14
        c.drawString(50, y, row)
    if inv.two_page:
        y -= 30
        c.setFont("Helvetica", 9)
        c.drawString(50, y, "Totals continue on page 2")
        c.showPage()
        y = h - 80
        c.setFont("Helvetica-Bold", 12)
        c.drawString(50, y, f"{SUPPLIERS[inv.sup]} - summary")
        y -= 10
    y -= 26
    c.setFont("Courier", 10)
    for t in totals:
        c.drawString(330, y, t)
        y -= 14
    y -= 20
    c.setFont("Helvetica", 9)
    c.drawString(50, y, "Payment terms: 30 days. Thank you for your business.")
    c.showPage()
    c.save()
    return buf.getvalue()


def render_scan_pdf(inv: Inv) -> bytes:
    """A page that is only a picture of the invoice: no text layer."""
    head, table, totals = text_lines(inv)
    img = Image.new("L", (1240, 1754), 255)
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=22)
    y = 80
    for ln in [SUPPLIERS[inv.sup], "", *head, "", *table, "", *totals]:
        d.text((70, y), ln, font=f, fill=40)
        y += 34
    img = img.rotate(1.2, fillcolor=255)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.drawImage(ImageReader(img), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    return buf.getvalue()


def main() -> None:
    inv_dir = OUT / "invoices"
    inv_dir.mkdir(exist_ok=True)
    for old in inv_dir.glob("*.pdf"):
        old.unlink()

    with open(OUT / "purchase_orders.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["po_number", "supplier", "line_no", "sku", "description", "quantity", "unit_price"])
        for po, (sup, lines) in POS.items():
            for i, (sku, desc, qty, price) in enumerate(lines, 1):
                w.writerow([po, SUPPLIERS[sup], i, sku, desc, qty, price])

    manifest = {"currency": CURRENCY, "buyer": BUYER, "invoices": []}
    for inv in INVOICES:
        if inv.kind == "scan":
            data = render_scan_pdf(inv)
        else:
            data = render_text_pdf(inv)
            if inv.kind == "corrupt":
                data = data[:700]
        (inv_dir / inv.filename).write_bytes(data)
        readable = inv.kind == "text"
        manifest["invoices"].append({
            "file": inv.filename,
            "category": inv.category,
            "readable": readable,
            "expected_status": "unreadable" if not readable else ("needs_review" if inv.expect else "clean"),
            "expected_codes": sorted(inv.expect),
            "expected_fields": None if not readable else {
                "supplier_name": SUPPLIERS[inv.sup],
                "invoice_number": inv.number,
                "invoice_date": inv.day.isoformat(),
                "po_number": inv.po_printed,
                "subtotal": str(inv.subtotal),
                "tax": str(inv.tax),
                "total": str(inv.total),
                "lines": [{"sku": ln.sku, "description": ln.description, "quantity": str(ln.quantity),
                           "unit_price": str(ln.unit_price), "line_total": str(ln.line_total)}
                          for ln in inv.lines],
            },
        })
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(INVOICES)} invoices, {len(POS)} purchase orders")


if __name__ == "__main__":
    main()
