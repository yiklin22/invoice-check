"""Drive a running server through the whole flow and print what it found.

Start the app first:  uv run uvicorn invoicecheck.app:app --port 8811
Then:                 uv run python scripts/demo.py http://127.0.0.1:8811

It creates two throwaway accounts, checks the fixtures in the first, checks
that the second can see none of it, and deletes both accounts at the end.
If the server needs an invite code, set INVOICECHECK_INVITE_CODE here too.
"""
import csv
import io
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent.parent / "fixtures"


def signed_up(base: str, name: str) -> tuple[httpx.Client, str]:
    client = httpx.Client(base_url=base, follow_redirects=True, timeout=60)
    password = secrets.token_urlsafe(12)
    r = client.post("/signup", data={"name": name, "email": f"demo-{secrets.token_hex(4)}@example.test",
                                     "password": password, "invite": os.environ.get("INVOICECHECK_INVITE_CODE", "")})
    if "Account created" not in r.text:
        sys.exit(f"could not create an account for {name}: HTTP {r.status_code}")
    return client, password


def export(client: httpx.Client, name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(client.get(f"/export/{name}.csv").text)))


def main(base: str) -> int:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())["invoices"]
    anonymous = httpx.get(base + "/export/invoices.csv", follow_redirects=False)
    print("without a login, the export answers:", anonymous.status_code, "->", anonymous.headers.get("location"))
    c, password = signed_up(base, "Demo reviewer")
    other, other_password = signed_up(base, "Second account")
    try:
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
        rows = export(c, "discrepancies")
        status = {r["file"]: r["status"] for r in export(c, "invoices")}
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
        total = len(export(c, "invoices"))
        print(f"\nre-upload of all {len(files)} files: {dupes} recognised as already uploaded, {total} invoice records in total")
        print(f"seeded cases surfaced as expected: {len(manifest) - wrong}/{len(manifest)}")

        first = state["items"][0]["invoice_id"]
        seen = [other.get(p).status_code for p in (f"/invoices/{first}", f"/invoices/{first}/pdf", f"/batches/{batch}")]
        tried = other.post(f"/invoices/{first}/delete").status_code
        leaked = len(export(other, "invoices")) + len(export(other, "discrepancies"))
        print(f"\nsecond account: {leaked} rows in its exports; the first account's invoice, PDF and upload "
              f"answer {seen}; deleting that invoice answers {tried}")
        isolated = leaked == 0 and set(seen) == {404} and tried == 404 and len(export(c, "invoices")) == total
        foreign = c.post(f"/invoices/{first}/delete", headers={"Origin": "https://elsewhere.example"}).status_code
        print(f"a delete sent from another site answers {foreign}; invoice records still held: {len(export(c, 'invoices'))}")
        isolated = isolated and foreign == 403 and len(export(c, "invoices")) == total
    finally:
        gone = [cl.post("/account/delete", data={"password": pw}).status_code for cl, pw in ((c, password), (other, other_password))]
        after = c.get("/export/invoices.csv", follow_redirects=False).status_code
        print(f"both demo accounts deleted: {gone}; the old login now answers {after}")
    return 1 if wrong or total != len(manifest) or not isolated or gone != [200, 200] or after != 303 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"))
