"""Web application: upload, processing, review with evidence, export."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import config, service
from .db import get_engine, get_session, new_session
from .matching import money, show
from .models import (APPROVED, CLEAN, NEEDS_REVIEW, OPEN, QUEUED, REJECTED, UNREADABLE, Invoice,
                     PurchaseOrder, UploadBatch)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "web")
_processing = threading.Lock()

STATUS_LABELS = {QUEUED: "Processing", UNREADABLE: "Unreadable", NEEDS_REVIEW: "Needs review",
                 CLEAN: "Clean", APPROVED: "Approved", REJECTED: "Rejected"}
CODE_LABELS = {
    "UNREADABLE": "File could not be read",
    "MISSING_FIELD": "Required value missing",
    "EVIDENCE_MISSING": "Value not found on the page",
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


def reviewer_name(request: Request) -> str:
    return (request.cookies.get("reviewer") or "").strip()[:80] or "reviewer"


def page(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("flash", request.query_params.get("msg") or request.query_params.get("err"))
    context.setdefault("flash_kind", "err" if request.query_params.get("err") else "ok")
    context["reviewer"] = reviewer_name(request)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    query = f"?msg={quote(msg)}" if msg else f"?err={quote(err)}" if err else ""
    return RedirectResponse(path + query, status_code=303)


def invoice_or_404(session: Session, invoice_id: int) -> Invoice:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "No such invoice")
    return inv


# --- pages -------------------------------------------------------------------

@app.get("/")
def index(request: Request, status: str = "", session: Session = Depends(get_session)):
    counts = dict(session.execute(select(Invoice.status, func.count()).group_by(Invoice.status)).all())
    query = select(Invoice).options(selectinload(Invoice.discrepancies)).order_by(Invoice.id)
    if status in STATUS_LABELS:
        query = query.where(Invoice.status == status)
    orders = session.scalar(select(func.count()).select_from(PurchaseOrder))
    return page(request, "index.html", invoices=session.scalars(query).all(), counts=counts,
                total=sum(counts.values()), current=status, orders=orders,
                max_mb=config.MAX_UPLOAD_BYTES // 1_000_000)


@app.post("/upload")
async def upload(background: BackgroundTasks, files: list[UploadFile], session: Session = Depends(get_session)):
    payload = []
    for f in files:
        # Read one byte past the limit so an oversized file is refused without holding all of it.
        payload.append((f.filename or "unnamed.pdf", await f.read(config.MAX_UPLOAD_BYTES + 1)))
    if not any(data for _, data in payload):
        return back("/", err="Choose at least one PDF file.")
    batch = service.create_batch(session, payload)
    background.add_task(process_in_background)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


def batch_state(session: Session, batch_id: int) -> dict:
    batch = session.get(UploadBatch, batch_id)
    if batch is None:
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
def batch_page(request: Request, batch_id: int, session: Session = Depends(get_session)):
    return page(request, "batch.html", batch=batch_state(session, batch_id))


@app.get("/api/batches/{batch_id}")
def batch_api(batch_id: int, session: Session = Depends(get_session)):
    return JSONResponse(batch_state(session, batch_id))


@app.get("/orders")
def orders(request: Request, errors: str = "", session: Session = Depends(get_session)):
    rows = session.scalars(select(PurchaseOrder).options(selectinload(PurchaseOrder.lines))
                           .order_by(PurchaseOrder.po_number)).all()
    return page(request, "orders.html", orders=rows, import_errors=[e for e in errors.split("|") if e])


@app.post("/orders/import")
async def orders_import(file: UploadFile, session: Session = Depends(get_session)):
    raw = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(raw) > config.MAX_UPLOAD_BYTES:
        return back("/orders", err="The order file is too large.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return back("/orders", err="The order file is not UTF-8 text.")
    result = service.import_purchase_orders(session, text)
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
def invoice_page(request: Request, invoice_id: int, session: Session = Depends(get_session)):
    return page(request, "invoice.html", **review_context(invoice_or_404(session, invoice_id)))


@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, session: Session = Depends(get_session)):
    inv = invoice_or_404(session, invoice_id)
    # The bytes are whatever was uploaded, so never let a browser guess their type.
    return Response(inv.pdf, media_type="application/pdf", headers={
        "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


# --- actions -----------------------------------------------------------------

@app.post("/invoices/{invoice_id}/edit")
async def invoice_edit(request: Request, invoice_id: int, session: Session = Depends(get_session)):
    invoice_or_404(session, invoice_id)
    form = await request.form()
    header = {f: str(form[f]) for f in service.HEADER_FIELDS if f in form}
    rows: dict[str, dict[str, str]] = {}
    for key, value in form.items():
        if key.startswith("line-"):                      # line-<n or newK>-<field>
            _, ref, field = key.split("-", 2)
            rows.setdefault(ref, {"line_no": ref if ref.isdigit() else ""})[field] = str(value)
    try:
        changed = service.apply_edit(session, invoice_id, reviewer_name(request), header,
                                     list(rows.values()), note=str(form.get("note", "")))
    except service.ReviewError as exc:
        session.rollback()
        return back(f"/invoices/{invoice_id}", err=str(exc))
    msg = f"Saved {changed} change(s) and re-ran matching." if changed else "Nothing was changed."
    return back(f"/invoices/{invoice_id}", msg=msg)


@app.post("/invoices/{invoice_id}/discrepancies")
def discrepancy_decision(request: Request, invoice_id: int, fingerprint: str = Form(), action: str = Form(),
                         note: str = Form(""), session: Session = Depends(get_session)):
    invoice_or_404(session, invoice_id)
    try:
        service.decide_discrepancy(session, invoice_id, fingerprint, action, reviewer_name(request), note)
    except service.ReviewError as exc:
        return back(f"/invoices/{invoice_id}", err=str(exc))
    return back(f"/invoices/{invoice_id}", msg="Decision recorded.")


@app.post("/invoices/{invoice_id}/decision")
def invoice_decision(request: Request, invoice_id: int, action: str = Form(), note: str = Form(""),
                     session: Session = Depends(get_session)):
    invoice_or_404(session, invoice_id)
    try:
        service.decide_invoice(session, invoice_id, action, reviewer_name(request), note)
    except service.ReviewError as exc:
        return back(f"/invoices/{invoice_id}", err=str(exc))
    return back(f"/invoices/{invoice_id}", msg=f"Invoice {action.rstrip('e')}ed.")


@app.post("/invoices/{invoice_id}/delete")
def invoice_delete(invoice_id: int, session: Session = Depends(get_session)):
    inv = invoice_or_404(session, invoice_id)
    name = inv.filename
    service.delete_invoice(session, invoice_id)
    return back("/", msg=f"Deleted {name} and everything derived from it.")


@app.post("/reviewer")
def set_reviewer(name: str = Form(), next: str = Form("/")):
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("reviewer", name.strip()[:80], max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    return response


@app.get("/export/discrepancies.csv")
def export_discrepancies(session: Session = Depends(get_session)):
    return Response(service.export_discrepancies_csv(session), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="discrepancies.csv"'})


@app.get("/export/invoices.csv")
def export_invoices(session: Session = Depends(get_session)):
    return Response(service.export_invoices_csv(session), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="invoices.csv"'})


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    session.execute(select(1))
    return {"ok": True}
