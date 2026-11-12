"""Login, per-account data, deletion, upload limits and usage caps."""
import csv
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from conftest import ACC, EMAIL, FIXTURES, PASSWORD, add_account, fixture_files, log_in
from invoicecheck import auth, config, service
from invoicecheck.models import (Account, Discrepancy, Invoice, InvoiceLine, LoginSession, PurchaseOrder,
                                 UploadBatch, UsageCounter, now)

ORDERS = (FIXTURES / "purchase_orders.csv").read_text()
PROTECTED_GETS = ["/", "/orders", "/account", "/invoices/1", "/invoices/1/pdf", "/batches/1",
                  "/export/invoices.csv", "/export/discrepancies.csv"]
WRITES = [("/invoices/1/edit", {"total": "1.00"}), ("/invoices/1/decision", {"action": "reject", "note": "x"}),
          ("/invoices/1/discrepancies", {"fingerprint": "x", "action": "dispute"}), ("/invoices/1/delete", {})]


def count(session, table, account_id=None):
    query = select(func.count()).select_from(table)
    return session.scalar(query if account_id is None else query.where(table.account_id == account_id))


def export(client, name="invoices"):
    return list(csv.DictReader(io.StringIO(client.get(f"/export/{name}.csv").text)))


@pytest.fixture
def other(loaded):
    """A second browser, logged in as Ben, while Ana's account holds the 20 fixtures."""
    from invoicecheck.app import app
    add_account(loaded, "ben@example.test", "Ben")
    with TestClient(app) as c:
        yield log_in(c, "ben@example.test")


# --- login -------------------------------------------------------------------

def test_password_is_stored_hashed_and_checked(session):
    account = auth.create_account(session, " New@Example.Test ", "New", "a long enough password")
    assert account.email == "new@example.test" and account.password_hash.startswith("scrypt$")
    assert "a long enough password" not in account.password_hash
    assert auth.verify_password("a long enough password", account.password_hash)
    assert not auth.verify_password("a long enough passwor", account.password_hash)
    assert not auth.verify_password("x", "not-a-hash")


def test_every_page_needs_a_login(loaded, anon):
    for path in PROTECTED_GETS:
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
    assert anon.get("/api/batches/1").status_code == 401
    for path, data in WRITES + [("/account/delete", {"password": PASSWORD}), ("/account/sample", {})]:
        r = anon.post(path, data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    r = anon.post("/upload", files=[("files", ("a.pdf", b"%PDF-1.4", "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert count(loaded, Invoice) == 20 and count(loaded, Account) == 1          # nothing was touched
    assert anon.get("/healthz").json() == {"ok": True}
    assert anon.get("/static/app.css").status_code == 200


def test_signup_logs_in_and_starts_with_an_empty_account(loaded, anon):
    r = anon.post("/signup", data={"name": "Cy", "email": "cy@example.test", "password": "another long one"})
    assert "Account created" in r.text and "cy@example.test" in r.text
    assert "No invoices" in anon.get("/").text and export(anon) == []
    session_row = loaded.scalar(select(LoginSession))
    assert session_row.token_hash != anon.cookies["session"] and len(session_row.token_hash) == 64


def test_signup_refusals(session, anon, monkeypatch):
    def signup(**over):
        data = {"name": "Cy", "email": "cy@example.test", "password": "another long one", **over}
        return anon.post("/signup", data=data)
    assert "Use a password of 10" in signup(password="short").text
    assert "Enter an email address" in signup(email="not-an-email").text
    assert "already exists" in signup(email=EMAIL.upper()).text
    monkeypatch.setattr(config, "INVITE_CODE", "let-me-in")
    assert "Invite code" in anon.get("/signup").text
    assert "invite code is not right" in signup(invite="guess").text
    monkeypatch.setattr(config, "MAX_ACCOUNTS", 1)
    assert "reached its limit on accounts" in signup(invite="let-me-in").text
    assert count(session, Account) == 1
    monkeypatch.setattr(config, "MAX_ACCOUNTS", 2)
    assert "Account created" in signup(invite="let-me-in").text


def test_wrong_password_then_throttle(session, anon, monkeypatch):
    monkeypatch.setattr(config, "MAX_LOGIN_FAILURES_PER_EMAIL", 3)
    for _ in range(3):
        r = anon.post("/login", data={"email": EMAIL, "password": "wrong"})
        assert r.status_code == 400 and "do not match" in r.text
    r = anon.post("/login", data={"email": EMAIL, "password": PASSWORD})         # the right one, too late
    assert r.status_code == 429 and "Too many failed attempts" in r.text
    assert anon.get("/", follow_redirects=False).status_code == 303
    r = anon.post("/login", data={"email": "nobody@example.test", "password": "wrong"})
    assert r.status_code == 400 and "do not match" in r.text                      # same answer for an unknown email


def test_logout_and_expiry_end_the_session(session, client):
    assert client.get("/account").status_code == 200
    session.execute(update(LoginSession).values(expires_at=now() - timedelta(seconds=1)))
    session.commit()
    assert client.get("/account", follow_redirects=False).status_code == 303
    log_in(client)
    token = client.cookies["session"]
    assert "You are logged out." in client.post("/logout").text
    client.cookies.set("session", token)                                         # a copied cookie is dead too
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_login_only_redirects_within_the_site(session, anon):
    for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
        r = anon.post("/login", data={"email": EMAIL, "password": PASSWORD, "next": target}, follow_redirects=False)
        assert r.headers["location"] == "/", target
    r = anon.post("/login", data={"email": EMAIL, "password": PASSWORD, "next": "/orders"}, follow_redirects=False)
    assert r.headers["location"] == "/orders"


# --- isolation ---------------------------------------------------------------

def test_another_account_sees_none_of_it(loaded, other):
    assert "No invoices" in other.get("/").text
    assert "No purchase orders loaded yet." in other.get("/orders").text
    assert export(other) == [] and export(other, "discrepancies") == []
    for path in ("/invoices/1", "/invoices/1/pdf", "/batches/1", "/api/batches/1"):
        assert other.get(path).status_code == 404, path
    before = [(i.status, i.total, len(i.decisions)) for i in loaded.scalars(select(Invoice).order_by(Invoice.id))]
    for path, data in WRITES:
        assert other.post(path, data=data).status_code == 404, path
    loaded.expire_all()
    assert [(i.status, i.total, len(i.decisions)) for i in loaded.scalars(select(Invoice).order_by(Invoice.id))] == before


def test_service_refuses_another_accounts_invoice(loaded):
    ben = add_account(loaded, "ben@example.test", "Ben")
    for call in (lambda: service.get_invoice(loaded, ben.id, 1),
                 lambda: service.apply_edit(loaded, ben.id, 1, "ben", {"total": "1.00"}, []),
                 lambda: service.decide_invoice(loaded, ben.id, 1, "reject", "ben", "x"),
                 lambda: service.delete_invoice(loaded, ben.id, 1)):
        with pytest.raises(LookupError):
            call()


def test_the_same_files_in_two_accounts_do_not_affect_each_other(loaded, other, manifest):
    mine_before = export_rows(loaded, ACC)
    # Ben uploads the identical 20 files and the identical order list.
    assert "Imported 13 orders" in other.post("/orders/import", files={"file": ("po.csv", ORDERS.encode(), "text/csv")}).text
    files = [("files", (n, data, "application/pdf")) for n, data in fixture_files()]
    batch = other.post("/upload", files=files, follow_redirects=False).headers["location"]
    items = other.get("/api" + batch).json()["items"]
    assert {i["outcome"] for i in items} == {"new"}                               # not "already uploaded"
    assert {i["filename"]: i["status"] for i in items} == {m["file"]: m["expected_status"] for m in manifest}
    got = sorted({(r["file"], r["code"]) for r in export(other, "discrepancies")})
    assert got == sorted((m["file"], c) for m in manifest for c in m["expected_codes"])
    # Ana's findings are what they were: Ben's copies are not her duplicates, and use none of her order quantity.
    loaded.expire_all()
    assert export_rows(loaded, ACC) == mine_before
    assert count(loaded, Invoice) == 40 and count(loaded, PurchaseOrder) == 26


def export_rows(session, account_id):
    return [{k: v for k, v in r.items() if k != "invoice_id"}
            for r in csv.DictReader(io.StringIO(service.export_discrepancies_csv(session, account_id)))]


def test_an_order_in_one_account_does_not_match_an_invoice_in_another(session):
    ben = add_account(session, "ben@example.test", "Ben")
    service.import_purchase_orders(session, ACC, ORDERS)                          # only Ana has the orders
    service.create_batch(session, ben.id, fixture_files({"01_clean_northwind.pdf"}))
    service.process_queued(session)
    inv = session.scalar(select(Invoice).where(Invoice.account_id == ben.id))
    assert [d.code for d in inv.discrepancies] == ["PO_NOT_FOUND"]


# --- deletion ----------------------------------------------------------------

def test_delete_all_data_needs_the_password_and_leaves_the_account(loaded, other, client):
    r = client.post("/account/delete-data", data={"password": "wrong"})
    assert "nothing was deleted" in r.text and count(loaded, Invoice, ACC) == 20
    other.post("/account/sample")                                                 # Ben has data of his own
    service.process_queued(loaded)
    r = client.post("/account/delete-data", data={"password": PASSWORD})
    assert "Deleted 20 invoice file(s) and 13 purchase order(s)" in r.text
    assert [count(loaded, t, ACC) for t in (Invoice, PurchaseOrder, UploadBatch)] == [0, 0, 0]
    assert count(loaded, InvoiceLine) > 0                                          # Ben's lines are still there
    assert count(loaded, Invoice) == 20 and len(export(other)) == 20
    assert client.get("/account").status_code == 200                              # still logged in


def test_delete_account_removes_everything_and_the_login(loaded, other, client):
    token = client.cookies["session"]
    assert "nothing was deleted" in client.post("/account/delete", data={"password": ""}).text
    r = client.post("/account/delete", data={"password": PASSWORD})
    assert "Your account and everything in it were deleted." in r.text
    for table in (Invoice, PurchaseOrder, UploadBatch, UsageCounter, LoginSession):
        assert count(loaded, table, ACC) == 0, table.__tablename__
    assert count(loaded, InvoiceLine) == 0 and count(loaded, Discrepancy) == 0
    assert loaded.scalars(select(Account.email)).all() == ["ben@example.test"]
    client.cookies.set("session", token)
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"email": EMAIL, "password": PASSWORD}).status_code == 400
    assert other.get("/account").status_code == 200


def test_sample_data_goes_into_the_callers_account_only(session, client, manifest):
    ben = add_account(session, "ben@example.test", "Ben")
    r = client.post("/account/sample", follow_redirects=False)
    state = client.get("/api" + r.headers["location"]).json()
    assert state["done"] and {i["filename"]: i["status"] for i in state["items"]} == {
        m["file"]: m["expected_status"] for m in manifest}
    assert count(session, Invoice, ACC) == 20 and count(session, Invoice, ben.id) == 0
    client.post("/account/sample")
    assert count(session, Invoice, ACC) == 20 and count(session, PurchaseOrder, ACC) == 13   # loading twice adds nothing


# --- limits and caps ---------------------------------------------------------

def outcomes(batch):
    return [(i.outcome, i.detail) for i in batch.items]


def test_cap_on_files_held(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_INVOICES", 2)
    batch = service.create_batch(session, ACC, fixture_files()[:3])
    assert [o for o, _ in outcomes(batch)] == ["new", "new", "rejected_upload"]
    assert "already holds 2 files" in batch.items[2].detail
    service.delete_invoice(session, ACC, batch.items[0].invoice_id)               # deleting makes room
    assert outcomes(service.create_batch(session, ACC, fixture_files()[2:3])) == [("new", None)]
    again = service.create_batch(session, ACC, fixture_files()[1:3])              # a re-upload is not refused
    assert {o for o, _ in outcomes(again)} == {"already_uploaded"}


def test_cap_on_storage(session, monkeypatch):
    first, second = fixture_files()[:2]
    monkeypatch.setattr(config, "MAX_STORAGE_BYTES", len(first[1]) + len(second[1]) - 1)
    batch = service.create_batch(session, ACC, [first, second])
    assert [o for o, _ in outcomes(batch)] == ["new", "rejected_upload"] and "storage is full" in batch.items[1].detail


def test_daily_cap_is_not_reset_by_deleting(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILES_PER_DAY", 2)
    batch = service.create_batch(session, ACC, fixture_files()[:2])
    service.delete_all_data(session, ACC)
    assert service.usage(session, ACC)["invoices"] == 0 and service.usage(session, ACC)["files_today"] == 2
    batch = service.create_batch(session, ACC, fixture_files()[2:3])
    assert [o for o, _ in outcomes(batch)] == ["rejected_upload"] and "new files a day" in batch.items[0].detail
    ben = add_account(session, "ben@example.test", "Ben")                         # another account has its own allowance
    assert outcomes(service.create_batch(session, ben.id, fixture_files()[:1])) == [("new", None)]
    session.execute(update(UsageCounter).values(day=UsageCounter.day - timedelta(days=1)))
    session.commit()                                                              # the next day
    assert outcomes(service.create_batch(session, ACC, fixture_files()[2:3])) == [("new", None)]


def test_cap_on_order_lines_rejects_the_whole_file(session, monkeypatch):
    monkeypatch.setattr(config, "MAX_ORDER_LINES", 5)
    result = service.import_purchase_orders(session, ACC, ORDERS)
    assert not result["ok"] and "the limit for an account is 5" in result["errors"][0]
    assert count(session, PurchaseOrder) == 0


def test_account_page_shows_usage_against_limits(loaded, client):
    html = client.get("/account").text
    assert "<td>Invoice files stored</td><td class=\"num\">20</td><td class=\"num\">200</td>" in html
    assert "<td>New files today (UTC)</td><td class=\"num\">20</td><td class=\"num\">100</td>" in html
    assert "Ana &middot; ana@example.test" in html


def test_oversized_and_unsized_requests_are_refused_unread(session, client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 2_000)
    r = client.post("/upload", files=[("files", ("big.pdf", b"x" * 3_000, "application/pdf"))])
    assert r.status_code == 413 and "larger than" in r.text
    assert client.post("/logout", content=iter([b"a", b"b"])).status_code == 411  # chunked, no length
    assert count(session, Invoice) == 0 and client.get("/account").status_code == 200
    monkeypatch.setattr(config, "MAX_ORDER_FILE_BYTES", 100)
    r = client.post("/orders/import", files={"file": ("po.csv", ORDERS.encode()[:1_000], "text/csv")})
    assert "The order file is larger than" in r.text


# --- requests from other sites ----------------------------------------------

def test_writes_from_another_site_are_refused(loaded, client):
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"}):
        r = client.post("/invoices/1/delete", headers=headers)
        assert r.status_code == 403, headers
    assert count(loaded, Invoice) == 20
    for headers in ({"Origin": "http://testserver"}, {"Sec-Fetch-Site": "same-origin"}):
        assert client.post("/invoices/1/decision", data={"action": "reopen"}, headers=headers).status_code == 200


def test_responses_carry_security_headers(session, client):
    r = client.get("/account")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    cookie = client.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False).headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    with TestClient(client.app, base_url="https://testserver") as https:
        cookie = https.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False).headers["set-cookie"]
        assert "Secure" in cookie
