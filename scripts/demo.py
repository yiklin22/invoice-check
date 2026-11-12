"""Drive a running server through the whole flow and print what it found.

Start the app first:  uv run uvicorn invoicecheck.app:app --port 8811
Then:                 uv run python scripts/demo.py http://127.0.0.1:8811
"""
import csv
import io
import json
import sys
import time
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent.parent / "fixtures"


def main(base: str) -> int:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())["invoices"]
    with httpx.Client(base_url=base, follow_redirects=True, timeout=60) as c:
        r = c.post("/orders/import", files={"file": ("purchase_orders.csv", (FIXTURES / "purchase_orders.csv").read_bytes())})
        print("orders import:", "ok" if "Imported" in r.text else "FAILED")
        files = [("files", (p.name, p.read_bytes(), "application/pdf"))
                 for p in sorted((FIXTURES / "invoices").glob("*.pdf"))]
        started = time.perf_counter()
        r = c.post("/upload", files=files)
        batch = str(r.url).rstrip("/").split("/")[-1]
        while not (state := c.get(f"/api/batches/{batch}").json())["done"]:
            time.sleep(0.2)
        print(f"upload {batch}: {len(state['items'])} files checked in {time.perf_counter() - started:.1f}s")
        rows = list(csv.DictReader(io.StringIO(c.get("/export/discrepancies.csv").text)))
        status = {r["file"]: r["status"] for r in csv.DictReader(io.StringIO(c.get("/export/invoices.csv").text))}
        wrong = 0
        print(f"\n{'file':40} {'status':13} findings")
        for m in manifest:
            codes = sorted({r["code"] for r in rows if r["file"] == m["file"]})
            ok = codes == m["expected_codes"] and status[m["file"]] == m["expected_status"]
            wrong += not ok
            print(f"{m['file']:40} {status[m['file']]:13} {', '.join(codes) or '-':45} {'' if ok else '<-- NOT AS SEEDED'}")
        again = c.post("/upload", files=files)
        items = c.get(f"/api/batches/{str(again.url).rstrip('/').split('/')[-1]}").json()["items"]
        dupes = sum(i["outcome"] == "already_uploaded" for i in items)
        total = len(list(csv.DictReader(io.StringIO(c.get("/export/invoices.csv").text))))
        print(f"\nre-upload of all {len(files)} files: {dupes} recognised as already uploaded, {total} invoice records in total")
        print(f"seeded cases surfaced as expected: {len(manifest) - wrong}/{len(manifest)}")
        return 1 if wrong or total != len(manifest) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"))
