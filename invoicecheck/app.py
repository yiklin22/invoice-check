"""Web application: accounts, upload, processing, review with evidence, export.

Every page except login, signup and the health check needs a logged-in
account, and every query is limited to that account's rows.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import auth, config, service
from .db import get_engine, get_session, new_session
from .matching import money, show
from .models import (APPROVED, CLEAN, NEEDS_REVIEW, OPEN, QUEUED, REJECTED, UNREADABLE, Account, Invoice,
                     PurchaseOrder, UploadBatch)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "web")
_processing = threading.Lock()
COOKIE = "session"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",          # the review page frames its own PDF; nobody else may frame anything
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; "
                               "form-action 'self'; base-uri 'none'",
}

STATUS_LABELS = {QUEUED: "Processing", UNREADABLE: "Unreadable", NEEDS_REVIEW: "Needs review",
                 CLEAN: "Clean", APPROVED: "Approved", REJECTED: "Rejected"}
CODE_LABELS = {
    "UNREADABLE": "File could not be read",
    "MISSING_FIELD": "Required value missing",
    "EVIDENCE_MISSING": "Value not found on the page",
    "MODEL_READ": "Read by a model",
    "PO_MISSING": "No purchase order on invoice",
    "PO_NOT_FOUND": "Purchase order not found",
    "AMBIGUOUS_PO": "Purchase order needs confirming",
    "AMBIGUOUS_SKU": "Item needs confirming",
    "SUPPLIER_MISMATCH": "Order belongs to another supplier",
    "SKU_NOT_ON_PO": "Item was not ordered",
    "PRICE_MISMATCH": "Price differs from order",
    "QTY_EXCEEDS_PO": "Quantity exceeds order",
    "LINE_ARITHMETIC": "Line total does not add up",
    "TOTAL_MISMATCH": "Invoice total does not add up",
    "DUPLICATE_INVOICE_NUMBER": "Invoice number already received",
    "POSSIBLE_DUPLICATE": "Possible duplicate billing",
}
FIELD_LABELS = {"supplier_name": "Supplier", "invoice_number": "Invoice number", "invoice_date": "Invoice date",
                "po_number": "Purchase order", "subtotal": "Subtotal", "tax": "Tax", "total": "Total",
                "line": "Line"}
templates.env.globals.update(status_labels=STATUS_LABELS, code_labels=CODE_LABELS, money=money, show=show)


class NotLoggedIn(Exception):
    pass


def process_in_background() -> None:
    """Read queued files. One run at a time in this process; the row locks
    in `process_queued` cover other processes."""
    with _processing:
        session = new_session()
        try:
            service.process_queued(session)
        finally:
            session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    process_in_background()     # pick up files left queued by a crash or restart
    yield


app = FastAPI(title="Invoice check", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "web" / "static"), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Refuse cross-site and oversized writes before anything reads the body,
    and put the same security headers on every response."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # A browser says where a request came from. A form on another site
        # must not be able to act with this site's login cookie.
        site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
        if site is not None:
            cross = site not in ("same-origin", "none")
        else:
            cross = origin is not None and urlsplit(origin).netloc != request.headers.get("host")
        if cross:
            return PlainTextResponse("Requests from other sites are not accepted.", status_code=403)
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return PlainTextResponse("A Content-Length header is required.", status_code=411)
        if int(length) > config.MAX_REQUEST_BYTES:
            return PlainTextResponse(
                f"The request is larger than {config.MAX_REQUEST_BYTES // 1_000_000} MB. Upload fewer files at a time.",
                status_code=413)
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def current_account(request: Request, session: Session = Depends(get_session)) -> Account:
    account = auth.account_for_token(session, request.cookies.get(COOKIE))
    if account is None:
        raise NotLoggedIn()
    request.state.account = account
    return account


@app.exception_handler(NotLoggedIn)
def not_logged_in(request: Request, exc: NotLoggedIn):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Log in first."}, status_code=401)
    target = request.url.path if request.method == "GET" else "/"
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


def page(request: Request, template: str, /, status_code: int = 200, **context):
    context.setdefault("flash", request.query_params.get("msg") or request.query_params.get("err"))
    context.setdefault("flash_kind", "err" if request.query_params.get("err") else "ok")
    context["account"] = getattr(request.state, "account", None)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    query = f"?msg={quote(msg)}" if msg else f"?err={quote(err)}" if err else ""
    return RedirectResponse(path + query, status_code=303)


def invoice_or_404(session: Session, account: Account, invoice_id: int) -> Invoice:
    """Another account's invoice gets the same answer as one that does not exist."""
    try:
        return service.get_invoice(session, account.id, invoice_id)
    except LookupError:
        raise HTTPException(404, "No such invoice") from None


def safe_next(target: str) -> str:
    """Only ever redirect to a path on this site."""
    ok = target.startswith("/") and not target.startswith("//") and "\\" not in target
    return target if ok else "/"


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE or request.url.scheme == "https")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# --- accounts ----------------------------------------------------------------

@app.get("/login")
def login_page(request: Request, next: str = "/", session: Session = Depends(get_session)):
    if auth.account_for_token(session, request.cookies.get(COOKIE)):
        return RedirectResponse(safe_next(next), status_code=303)
    return page(request, "login.html", next=safe_next(next), email="")


@app.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/"),
          session: Session = Depends(get_session)):
    try:
        token = auth.log_in(session, email, password, client_ip(request))
    except auth.AuthError as exc:
        return page(request, "login.html", status_code=exc.status_code, flash=str(exc), flash_kind="err",
                    next=safe_next(next), email=email[:254])
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(request, response, token)
    return response


@app.get("/signup")
def signup_page(request: Request):
    return page(request, "signup.html", invite_needed=bool(config.INVITE_CODE), name="", email="",
                min_password=config.MIN_PASSWORD_CHARS)


@app.post("/signup")
def signup(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""),
           invite: str = Form(""), session: Session = Depends(get_session)):
    try:
        auth.create_account(session, email, name, password, invite)
        token = auth.log_in(session, email, password, client_ip(request))
    except auth.AuthError as exc:
        return page(request, "signup.html", status_code=exc.status_code, flash=str(exc), flash_kind="err",
                    invite_needed=bool(config.INVITE_CODE), name=name[:80], email=email[:254],
                    min_password=config.MIN_PASSWORD_CHARS)
    response = back("/account", msg="Account created. Nothing is in it yet.")
    set_session_cookie(request, response, token)
    return response


@app.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    auth.log_out(session, request.cookies.get(COOKIE))
    response = back("/login", msg="You are logged out.")
    response.delete_cookie(COOKIE)
    return response


@app.get("/account")
def account_page(request: Request, account: Account = Depends(current_account),
                 session: Session = Depends(get_session)):
    return page(request, "account.html", usage=service.usage(session, account.id),
                sample=service.sample_available(), max_mb=config.MAX_UPLOAD_BYTES // 1_000_000,
                max_files=config.MAX_FILES_PER_UPLOAD)


@app.post("/account/sample")
def account_sample(background: BackgroundTasks, account: Account = Depends(current_account),
                   session: Session = Depends(get_session)):
    if not service.sample_available():
        raise HTTPException(404, "No sample data is installed")
    try:
        batch = service.load_sample_data(session, account.id)
    except service.ReviewError as exc:
        return back("/account", err=str(exc))
    background.add_task(process_in_background)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@app.post("/account/delete-data")
def account_delete_data(password: str = Form(""), account: Account = Depends(current_account),
                        session: Session = Depends(get_session)):
    try:
        auth.check_password(account, password)
    except auth.AuthError as exc:
        return back("/account", err=str(exc))
    removed = service.delete_all_data(session, account.id)
    return back("/account", msg=f"Deleted {removed['invoices']} invoice file(s) and {removed['orders']} "
                                f"purchase order(s). Your account is still here.")


@app.post("/account/delete")
def account_delete(password: str = Form(""), account: Account = Depends(current_account),
                   session: Session = Depends(get_session)):
    try:
        auth.check_password(account, password)
    except auth.AuthError as exc:
        return back("/account", err=str(exc))
    service.delete_account(session, account.id)
    response = back("/login", msg="Your account and everything in it were deleted.")
    response.delete_cookie(COOKIE)
    return response


# --- pages -------------------------------------------------------------------

@app.get("/")
def index(request: Request, status: str = "", account: Account = Depends(current_account),
          session: Session = Depends(get_session)):
    mine = Invoice.account_id == account.id
    counts = dict(session.execute(select(Invoice.status, func.count()).where(mine).group_by(Invoice.status)).all())
    query = select(Invoice).where(mine).options(selectinload(Invoice.discrepancies)).order_by(Invoice.id)
    if status in STATUS_LABELS:
        query = query.where(Invoice.status == status)
    orders = session.scalar(select(func.count()).select_from(PurchaseOrder)
                            .where(PurchaseOrder.account_id == account.id))
    return page(request, "index.html", invoices=session.scalars(query).all(), counts=counts,
                total=sum(counts.values()), current=status, orders=orders,
                max_mb=config.MAX_UPLOAD_BYTES // 1_000_000)


@app.post("/upload")
async def upload(background: BackgroundTasks, files: list[UploadFile],
                 account: Account = Depends(current_account), session: Session = Depends(get_session)):
    payload = []
    for f in files:
        # Read one byte past the limit so an oversized file is refused without holding all of it.
        payload.append((f.filename or "unnamed.pdf", await f.read(config.MAX_UPLOAD_BYTES + 1)))
    if not any(data for _, data in payload):
        return back("/", err="Choose at least one PDF file.")
    batch = service.create_batch(session, account.id, payload)
    background.add_task(process_in_background)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


def batch_state(session: Session, account: Account, batch_id: int) -> dict:
    batch = session.get(UploadBatch, batch_id)
    if batch is None or batch.account_id != account.id:
        raise HTTPException(404, "No such upload")
    items = []
    for item in batch.items:
        inv = item.invoice
        items.append({
            "filename": item.filename, "outcome": item.outcome, "detail": item.detail,
            "invoice_id": inv.id if inv else None,
            "status": inv.status if inv else None,
            "status_label": STATUS_LABELS[inv.status] if inv else "Not accepted",
            "discrepancies": len(inv.discrepancies) if inv else 0})
    return {"id": batch.id, "done": all(i["status"] != QUEUED for i in items), "items": items}


@app.get("/batches/{batch_id}")
def batch_page(request: Request, batch_id: int, account: Account = Depends(current_account),
               session: Session = Depends(get_session)):
    return page(request, "batch.html", batch=batch_state(session, account, batch_id))


@app.get("/api/batches/{batch_id}")
def batch_api(batch_id: int, account: Account = Depends(current_account),
              session: Session = Depends(get_session)):
    return JSONResponse(batch_state(session, account, batch_id))


@app.get("/orders")
def orders(request: Request, errors: str = "", account: Account = Depends(current_account),
           session: Session = Depends(get_session)):
    rows = session.scalars(select(PurchaseOrder).where(PurchaseOrder.account_id == account.id)
                           .options(selectinload(PurchaseOrder.lines)).order_by(PurchaseOrder.po_number)).all()
    return page(request, "orders.html", orders=rows, import_errors=[e for e in errors.split("|") if e])


@app.post("/orders/import")
async def orders_import(file: UploadFile, account: Account = Depends(current_account),
                        session: Session = Depends(get_session)):
    raw = await file.read(config.MAX_ORDER_FILE_BYTES + 1)
    if len(raw) > config.MAX_ORDER_FILE_BYTES:
        return back("/orders", err=f"The order file is larger than {config.MAX_ORDER_FILE_BYTES // 1000:,} kB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return back("/orders", err="The order file is not UTF-8 text.")
    result = service.import_purchase_orders(session, account.id, text)
    if not result["ok"]:
        shown = "|".join(result["errors"][:20])
        return RedirectResponse(f"/orders?err={quote('Nothing was imported.')}&errors={quote(shown)}", 303)
    return back("/orders", msg=f"Imported {result['orders']} orders ({result['lines']} lines).")


def review_context(inv: Invoice) -> dict:
    """Arrange an invoice for the side-by-side review page."""
    header_ev = {e.field: e for e in inv.evidence if e.line_no is None}
    fields = [{"name": f, "label": FIELD_LABELS[f], "value": getattr(inv, f), "evidence": header_ev.get(f)}
              for f in service.HEADER_FIELDS]
    line_ev = {e.line_no: e for e in inv.evidence if e.field == "line"}
    edited = {e.line_no for e in inv.evidence if e.field.startswith("line.")}
    lines = [{"row": ln, "evidence": line_ev.get(ln.line_no), "edited": ln.line_no in edited} for ln in inv.lines]
    tags: dict[tuple[int, int], list[str]] = {}
    for e in inv.evidence:
        if e.located:
            label = FIELD_LABELS.get(e.field, e.field) + (f" {e.line_no}" if e.line_no else "")
            tags.setdefault((e.page, e.page_line), []).append(label)
    pages = [{"no": p, "lines": [{"no": i, "text": text, "tags": tags.get((p, i), [])}
                                 for i, text in enumerate(body.splitlines(), 1) if text.strip()]}
             for p, body in enumerate(inv.page_texts or [], 1)]
    undecided = sum(d.state != "accepted" for d in inv.discrepancies)
    return {"inv": inv, "fields": fields, "lines": lines, "pages": pages, "undecided": undecided,
            "can_decide": inv.status == NEEDS_REVIEW,
            "can_approve": inv.status in (NEEDS_REVIEW, CLEAN) and undecided == 0,
            "can_reopen": inv.status in (APPROVED, REJECTED),
            "impact": sum((d.impact for d in inv.discrepancies if d.impact and d.state == OPEN), 0)}


@app.get("/invoices/{invoice_id}")
def invoice_page(request: Request, invoice_id: int, account: Account = Depends(current_account),
                 session: Session = Depends(get_session)):
    return page(request, "invoice.html", **review_context(invoice_or_404(session, account, invoice_id)))


@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, account: Account = Depends(current_account),
                session: Session = Depends(get_session)):
    inv = invoice_or_404(session, account, invoice_id)
    # The bytes are whatever was uploaded, so never let a browser guess their type.
    return Response(inv.pdf, media_type="application/pdf", headers={
        "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


# --- actions -----------------------------------------------------------------

@app.post("/invoices/{invoice_id}/edit")
async def invoice_edit(request: Request, invoice_id: int, account: Account = Depends(current_account),
                       session: Session = Depends(get_session)):
    invoice_or_404(session, account, invoice_id)
    form = await request.form()
    header = {f: str(form[f]) for f in service.HEADER_FIELDS if f in form}
    rows: dict[str, dict[str, str]] = {}
    for key, value in form.items():
        if key.startswith("line-"):                      # line-<n or newK>-<field>
            _, ref, field = key.split("-", 2)
            rows.setdefault(ref, {"line_no": ref if ref.isdigit() else ""})[field] = str(value)
    try:
        changed = service.apply_edit(session, account.id, invoice_id, account.name, header,
                                     list(rows.values()), note=str(form.get("note", "")))
    except service.ReviewError as exc:
        session.rollback()
        return back(f"/invoices/{invoice_id}", err=str(exc))
    msg = f"Saved {changed} change(s) and re-ran matching." if changed else "Nothing was changed."
    return back(f"/invoices/{invoice_id}", msg=msg)


@app.post("/invoices/{invoice_id}/discrepancies")
def discrepancy_decision(invoice_id: int, fingerprint: str = Form(), action: str = Form(),
                         note: str = Form(""), account: Account = Depends(current_account),
                         session: Session = Depends(get_session)):
    invoice_or_404(session, account, invoice_id)
    try:
        service.decide_discrepancy(session, account.id, invoice_id, fingerprint, action, account.name, note)
    except service.ReviewError as exc:
        return back(f"/invoices/{invoice_id}", err=str(exc))
    return back(f"/invoices/{invoice_id}", msg="Decision recorded.")


@app.post("/invoices/{invoice_id}/decision")
def invoice_decision(invoice_id: int, action: str = Form(), note: str = Form(""),
                     account: Account = Depends(current_account), session: Session = Depends(get_session)):
    invoice_or_404(session, account, invoice_id)
    try:
        service.decide_invoice(session, account.id, invoice_id, action, account.name, note)
    except service.ReviewError as exc:
        return back(f"/invoices/{invoice_id}", err=str(exc))
    return back(f"/invoices/{invoice_id}", msg=f"Invoice {action.rstrip('e')}ed.")


@app.post("/invoices/{invoice_id}/delete")
def invoice_delete(invoice_id: int, account: Account = Depends(current_account),
                   session: Session = Depends(get_session)):
    inv = invoice_or_404(session, account, invoice_id)
    name = inv.filename
    service.delete_invoice(session, account.id, invoice_id)
    return back("/", msg=f"Deleted {name} and everything derived from it.")


@app.get("/export/discrepancies.csv")
def export_discrepancies(account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return Response(service.export_discrepancies_csv(session, account.id), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="discrepancies.csv"'})


@app.get("/export/invoices.csv")
def export_invoices(account: Account = Depends(current_account), session: Session = Depends(get_session)):
    return Response(service.export_invoices_csv(session, account.id), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="invoices.csv"'})


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    session.execute(select(1))
    return {"ok": True}
