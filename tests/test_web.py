"""The screens, driven over HTTP against PostgreSQL."""
import csv
import io

from conftest import FIXTURES, fixture_files


def upload(client, names=None):
    files = [("files", (n, data, "application/pdf")) for n, data in fixture_files(names)]
    return client.post("/upload", files=files, follow_redirects=False)


def import_orders(client):
    return client.post("/orders/import", files={"file": ("po.csv", (FIXTURES / "purchase_orders.csv").read_bytes(), "text/csv")})


def test_upload_to_export_flow(client, manifest):
    assert "Imported 13 orders" in import_orders(client).text
    r = upload(client)
    assert r.status_code == 303 and r.headers["location"] == "/batches/1"
    state = client.get("/api/batches/1").json()
    assert state["done"] and len(state["items"]) == 20
    got = {i["filename"]: i["status"] for i in state["items"]}
    assert got == {m["file"]: m["expected_status"] for m in manifest}
    assert "All files are checked." in client.get("/batches/1").text

    home = client.get("/").text
    assert "Needs review" in home and "Unreadable" in home and "Price differs from order" in home
    assert client.get("/?status=clean").text.count('class="badge st-clean"') == 7

    rows = list(csv.DictReader(io.StringIO(client.get("/export/discrepancies.csv").text)))
    want = sorted((m["file"], c) for m in manifest for c in m["expected_codes"])
    assert sorted({(r["file"], r["code"]) for r in rows}) == want


def test_reupload_points_at_existing_records(client):
    upload(client, {"01_clean_northwind.pdf"})
    upload(client, {"01_clean_northwind.pdf", "02_clean_kestrel.pdf"})
    items = client.get("/api/batches/2").json()["items"]
    assert [(i["outcome"], i["invoice_id"]) for i in items] == [("already_uploaded", 1), ("new", 2)]
    assert len(list(csv.DictReader(io.StringIO(client.get("/export/invoices.csv").text)))) == 2


def test_review_page_shows_evidence_beside_the_document(client):
    import_orders(client)
    upload(client, {"06_clean_bluegum_two_pages.pdf"})
    html = client.get("/invoices/1").text
    assert '<iframe class="pdf" src="/invoices/1/pdf"' in html
    assert 'href="#p2l7"' in html and 'id="p2l7" class="cited"' in html       # total is cited to page 2
    assert "page 1, line 4" in html and "TOTAL DUE     $1,316.70" in html
    pdf = client.get("/invoices/1/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.headers["x-content-type-options"] == "nosniff"
    assert pdf.content.startswith(b"%PDF")


def test_unreadable_page_says_so_and_cannot_be_approved(client):
    upload(client, {"18_unreadable_scan.pdf"})
    html = client.get("/invoices/1").text
    assert "This file could not be read." in html and "no text layer" in html
    assert 'value="approve" disabled' in html
    r = client.post("/invoices/1/decision", data={"action": "approve"})
    assert "cannot be approved" in r.text and 'class="badge st-unreadable"' in r.text


def test_edit_decide_and_approve_through_forms(client):
    import_orders(client)
    upload(client, {"08_ambiguous_po_format.pdf", "11_price_above_po.pdf"})
    r = client.post("/invoices/1/edit", data={"po_number": "PO-1007", "note": "Confirmed with buyer"})
    assert "Saved 1 change(s)" in r.text and "typed by Ana" in r.text and "No discrepancies found." in r.text
    assert "Invoice approved." in client.post("/invoices/1/decision", data={"action": "approve"}).text

    page = client.get("/invoices/2").text
    fingerprint = page.split('name="fingerprint" value="')[1].split('"')[0]
    r = client.post("/invoices/2/discrepancies", data={"fingerprint": fingerprint, "action": "accept", "note": ""})
    assert "Say why the discrepancy is acceptable." in r.text
    client.post("/invoices/2/discrepancies", data={"fingerprint": fingerprint, "action": "accept", "note": "New price list"})
    assert "Invoice approved." in client.post("/invoices/2/decision", data={"action": "approve"}).text
    r = client.post("/invoices/2/edit", data={"line-1-quantity": "13"})
    assert 'class="badge st-needs_review"' in r.text and "approval invalidated" in r.text


def test_bad_values_and_bad_order_files_are_refused_with_a_message(client):
    import_orders(client)
    upload(client, {"02_clean_kestrel.pdf"})
    r = client.post("/invoices/1/edit", data={"total": "about 1400"})
    assert "is not a number" in r.text and 'class="badge st-clean"' in r.text
    r = client.post("/orders/import", files={"file": ("po.csv", b"po_number,supplier\nPO-1,Acme\n", "text/csv")})
    assert "Nothing was imported." in r.text and "Missing column(s)" in r.text


def test_delete_and_misc(client):
    upload(client, {"01_clean_northwind.pdf"})
    assert "Deleted 01_clean_northwind.pdf" in client.post("/invoices/1/delete").text
    assert client.get("/invoices/1").status_code == 404
    assert client.get("/healthz").json() == {"ok": True}
    assert "No purchase orders loaded yet." in client.get("/orders").text
