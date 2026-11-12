"""Pilot: read a few fixture invoices with the real model, ignoring the supplier templates, and compare
with the fixture manifest.

THIS SPENDS MONEY on the API key in the environment. It stops before any
call once the estimated spend reaches --max-usd.

    uv run --env-file .env python scripts/model_pilot.py            # 6 invoices, stops at $0.50
    uv run --env-file .env python scripts/model_pilot.py --files 01,07 --max-usd 0.10
"""
import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from invoicecheck import config, model                 # noqa: E402
from invoicecheck.extraction import read_pages          # noqa: E402

USD_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
                "claude-haiku-5-5": (0.10, 0.50)}       # input, output; list prices, November 2026
AMOUNTS = ("subtotal", "tax", "total")
LINE_AMOUNTS = ("quantity", "unit_price", "line_total")


def differences(got, want) -> list[str]:
    if not got.readable:
        return [f"not read: {got.reason}"]
    out = []
    for field in ("supplier_name", "invoice_number", "po_number"):
        if getattr(got, field) != want.get(field):
            out.append(f"{field}: got {getattr(got, field)!r}, expected {want.get(field)!r}")
    if str(got.invoice_date) != str(want.get("invoice_date")):
        out.append(f"invoice_date: got {got.invoice_date}, expected {want.get('invoice_date')}")
    for field in AMOUNTS:
        expected = Decimal(want[field]) if want.get(field) is not None else None
        if getattr(got, field) != expected:
            out.append(f"{field}: got {getattr(got, field)}, expected {expected}")
    if len(got.lines) != len(want["lines"]):
        return out + [f"lines: got {len(got.lines)}, expected {len(want['lines'])}"]
    for line, expected in zip(got.lines, want["lines"]):
        if line.sku != expected["sku"] or any(getattr(line, f) != Decimal(expected[f]) for f in LINE_AMOUNTS):
            out.append(f"line {line.line_no}: got {line.sku} {line.quantity} x {line.unit_price} = {line.line_total}, "
                       f"expected {expected['sku']} {expected['quantity']} x {expected['unit_price']} = {expected['line_total']}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", default="01,03,05,08,12,20", help="two-digit fixture numbers")
    parser.add_argument("--max-usd", type=float, default=0.50)
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Put it in .env and run with: uv run --env-file .env python scripts/model_pilot.py")
        return 2
    if config.MODEL not in USD_PER_MTOK:
        print(f"No price on file for {config.MODEL}; add it to USD_PER_MTOK so the spend limit works.")
        return 2
    usd_in, usd_out = USD_PER_MTOK[config.MODEL]
    manifest = {m["file"][:2]: m for m in json.loads((ROOT / "fixtures" / "manifest.json").read_text())["invoices"]}
    reader = model.ModelReader()

    spent, agreed, done, unlocated = 0.0, 0, 0, 0
    print(f"model {config.MODEL}, effort {config.MODEL_EFFORT}, spend limit ${args.max_usd:.2f}\n")
    for number in args.files.split(","):
        entry = manifest[number]
        if not entry["readable"]:
            print(f"{entry['file']}: skipped, the fixture has no text to read")
            continue
        if spent >= args.max_usd:
            print(f"Stopped before {entry['file']}: estimated spend ${spent:.4f} reached the limit.")
            break
        pages, started = read_pages(str(ROOT / "fixtures" / "invoices" / entry["file"])), time.monotonic()
        reader.usage = None
        got = reader.read(pages)
        took, u, done = time.monotonic() - started, reader.usage, done + 1
        cost = (u.input_tokens * usd_in + u.output_tokens * usd_out) / 1_000_000 if u else 0.0
        spent += cost
        diffs = differences(got, entry["expected_fields"])
        agreed += not diffs
        missing = [e.field if e.line_no is None else f"line {e.line_no}" for e in got.evidence if e.raw_text and not e.located]
        unlocated += len(missing)
        tokens = f"{u.input_tokens:>6} in {u.output_tokens:>5} out" if u else "no usage reported".rjust(21)
        print(f"{entry['file']:<34} {'same as manifest' if not diffs else 'DIFFERENT':<17}{tokens}  ${cost:.4f}  {took:4.1f}s")
        for line in diffs:
            print(f"      {line}")
        if missing:
            print(f"      not found on the page: {', '.join(missing)}")
    print(f"\n{agreed} of {done} read as the manifest says. {unlocated} value(s) not found on the page.")
    print(f"Estimated spend ${spent:.4f}, from token counts at list price; the Console shows what was billed.")
    print("These are invented invoices in three layouts the templates already read, so this checks that the "
          "model step works and what a call costs. It does not show accuracy on real invoices.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
