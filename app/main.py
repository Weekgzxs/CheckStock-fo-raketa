import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, stock
from app.stock import Shortage, StockError


@asynccontextmanager
async def lifespan(_app):
    with db.connect() as conn:
        db.init_schema(conn)
        if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            stock.seed(conn)
    yield


app = FastAPI(title="CheckStock", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(STATUS=stock.STATUS, new_id=uuid.uuid4)

DUPLICATE = "Повторная отправка: операция уже проведена, остатки не изменились"


def redirect(url, **params):
    q = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{url}?{q}" if q else url, status_code=303)


def page(request, name, status_code=200, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def act(back, fn, done):
    """Run one operation in its own transaction; business errors roll it back and go to the flash."""
    try:
        with db.connect() as conn:
            created = fn(conn)
    except StockError as e:
        return redirect(back, err=str(e))
    return redirect(back, msg=done if created else DUPLICATE)


# ---------- screens ----------

@app.get("/")
def overview(request: Request):
    with db.connect() as conn:
        return page(request, "overview.html", **stock.overview(conn))


@app.get("/ops")
def ops(request: Request):
    with db.connect() as conn:
        return page(request, "ops.html", **stock.choices(conn))


@app.get("/warehouses")
def warehouses(request: Request):
    with db.connect() as conn:
        return page(request, "warehouses.html", warehouses=stock.warehouses(conn))


@app.get("/history")
def history(request: Request):
    with db.connect() as conn:
        return page(request, "history.html", rows=stock.history(conn))


# ---------- operations ----------

@app.post("/ops/receipt")
def receipt(wh: int = Form(), sku: str = Form(), qty: int = Form(), request_id: uuid.UUID = Form()):
    return act("/ops", lambda c: stock.receive(c, wh, sku, qty, request_id), "Приход оформлен")


@app.post("/ops/transfer")
def transfer(from_wh: int = Form(), to_wh: int = Form(), sku: str = Form(), qty: int = Form(),
             request_id: uuid.UUID = Form()):
    return act("/ops", lambda c: stock.transfer_send(c, from_wh, to_wh, sku, qty, request_id),
               "Перемещение отправлено, товар в пути")


@app.post("/ops/{kind}")
def outbound(request: Request, kind: Literal["fbs", "fbo"], wh: int = Form(), sku: str = Form(),
             qty: int = Form(), request_id: uuid.UUID = Form(), split: bool = Form(False)):
    try:
        with db.connect() as conn:
            ids = stock.outbound(conn, kind, wh, sku, qty, request_id, split)
    except Shortage as e:
        # Re-render the form with ready-made ways out instead of a bare error.
        with db.connect() as conn:
            return page(request, "ops.html", 409, err=str(e), shortage=e,
                        form={"kind": kind, "wh": wh, "sku": sku, "qty": qty, "request_id": request_id},
                        **stock.choices(conn))
    except StockError as e:
        return redirect("/ops", err=str(e))
    if not ids:
        return redirect("/ops", msg=DUPLICATE)
    what = "Резерв FBS создан" if kind == "fbs" else "Отгрузка FBO оформлена, товар в пути"
    return redirect("/", msg=f"{what}: " + ", ".join(f"#{i}" for i in ids))


@app.post("/docs/{doc_id}/{action}")
def confirm(doc_id: int, action: Literal["accept", "ship", "cancel"]):
    try:
        with db.connect() as conn:
            changed, status = stock.confirm(conn, doc_id, action)
    except StockError as e:
        return redirect("/", err=str(e))
    if changed:
        return redirect("/", msg=f"Документ #{doc_id}: {stock.STATUS[status]}")
    return redirect("/", msg=f"Документ #{doc_id} уже обработан ({stock.STATUS[status]}), повторно ничего не списано")


# ---------- warehouses ----------

@app.post("/warehouses")
def add_warehouse(name: str = Form()):
    return act("/warehouses", lambda c: stock.add_warehouse(c, name), "ФФ добавлен")


@app.post("/warehouses/{wh_id}/rename")
def rename_warehouse(wh_id: int, name: str = Form()):
    return act("/warehouses", lambda c: stock.rename_warehouse(c, wh_id, name) or True, "ФФ переименован")


@app.post("/warehouses/{wh_id}/delete")
def delete_warehouse(wh_id: int):
    return act("/warehouses", lambda c: stock.delete_warehouse(c, wh_id) or True,
               "ФФ удалён. Остатки и история сохранены, остаток можно переместить на другой ФФ")


@app.post("/reset")
def reset():
    return act("/", lambda c: stock.seed(c) or True, "Данные сброшены к тестовым")
