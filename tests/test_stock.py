import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db, stock
from app.main import app
from app.stock import Shortage, StockError

NORTH, SOUTH = 1, 2  # seed order


def run(conn, fn, *args):
    """One operation = one transaction, rolled back on error, exactly like an HTTP request."""
    with conn.transaction():
        return fn(conn, *args)


def at(conn, wh, sku):
    row = conn.execute(
        "SELECT on_hand, reserved FROM stock WHERE warehouse_id = %s AND sku = %s", (wh, sku)
    ).fetchone()
    return (row["on_hand"], row["reserved"]) if row else (0, 0)


def summary(conn, sku):
    rows = stock.overview(conn)["summary"]
    assert all(r["balanced"] for r in rows), rows  # the invariant must hold after every action
    return next(r for r in rows if r["sku"] == sku)


def test_seed_matches_assignment(conn):
    assert at(conn, NORTH, "SKU-101") == (10, 0) and at(conn, SOUTH, "SKU-101") == (6, 0)
    assert at(conn, NORTH, "SKU-102") == (5, 0) and at(conn, SOUTH, "SKU-102") == (0, 0)
    assert at(conn, NORTH, "SKU-103") == (0, 0) and at(conn, SOUTH, "SKU-103") == (4, 0)
    assert summary(conn, "SKU-101")["received"] == 16


def test_receipt_double_submit_counts_once(conn):
    rid = uuid4()
    assert run(conn, stock.receive, NORTH, "SKU-103", 3, rid)
    assert run(conn, stock.receive, NORTH, "SKU-103", 3, rid) is None
    assert at(conn, NORTH, "SKU-103") == (3, 0)
    assert summary(conn, "SKU-103")["received"] == 7


def test_fbs_reserve_then_ship(conn):
    [doc] = run(conn, stock.outbound, "fbs", NORTH, "SKU-101", 4, uuid4())
    assert at(conn, NORTH, "SKU-101") == (10, 4)  # still on the shelf, 6 available
    s = summary(conn, "SKU-101")
    assert (s["on_hand"], s["available"], s["reserved"]) == (16, 12, 4)

    assert run(conn, stock.confirm, doc, "ship") == (True, "shipped")
    assert at(conn, NORTH, "SKU-101") == (6, 0)
    assert summary(conn, "SKU-101")["fbs_shipped"] == 4

    # repeated confirm and a late cancel change nothing
    assert run(conn, stock.confirm, doc, "ship") == (False, "shipped")
    assert run(conn, stock.confirm, doc, "cancel") == (False, "shipped")
    assert at(conn, NORTH, "SKU-101") == (6, 0)


def test_fbs_cancel_releases_reserve(conn):
    [doc] = run(conn, stock.outbound, "fbs", NORTH, "SKU-101", 3, uuid4())
    assert run(conn, stock.confirm, doc, "cancel") == (True, "cancelled")
    assert run(conn, stock.confirm, doc, "cancel") == (False, "cancelled")
    assert run(conn, stock.confirm, doc, "ship") == (False, "cancelled")
    assert at(conn, NORTH, "SKU-101") == (10, 0)
    summary(conn, "SKU-101")


def test_reserve_blocks_stock_for_other_operations(conn):
    run(conn, stock.outbound, "fbs", NORTH, "SKU-102", 5, uuid4())
    with pytest.raises(Shortage):
        run(conn, stock.outbound, "fbo", NORTH, "SKU-102", 1, uuid4())
    with pytest.raises(Shortage):
        run(conn, stock.transfer_send, NORTH, SOUTH, "SKU-102", 1, uuid4())
    assert at(conn, NORTH, "SKU-102") == (5, 5)


def test_transfer_send_and_accept(conn):
    doc = run(conn, stock.transfer_send, NORTH, SOUTH, "SKU-101", 4, uuid4())
    assert at(conn, NORTH, "SKU-101") == (6, 0) and at(conn, SOUTH, "SKU-101") == (6, 0)
    assert summary(conn, "SKU-101")["transit_ff"] == 4

    assert run(conn, stock.confirm, doc, "accept") == (True, "received")
    assert run(conn, stock.confirm, doc, "accept") == (False, "received")
    assert at(conn, SOUTH, "SKU-101") == (10, 0)
    assert summary(conn, "SKU-101")["transit_ff"] == 0


def test_transfer_to_warehouse_without_row_creates_it(conn):
    doc = run(conn, stock.transfer_send, NORTH, SOUTH, "SKU-102", 2, uuid4())
    run(conn, stock.confirm, doc, "accept")
    assert at(conn, SOUTH, "SKU-102") == (2, 0)


def test_fbo_leaves_ff_and_is_in_transit_until_accepted(conn):
    [doc] = run(conn, stock.outbound, "fbo", SOUTH, "SKU-103", 3, uuid4())
    assert at(conn, SOUTH, "SKU-103") == (1, 0)
    assert summary(conn, "SKU-103")["fbo_transit"] == 3
    assert run(conn, stock.confirm, doc, "accept") == (True, "accepted")
    assert run(conn, stock.confirm, doc, "accept") == (False, "accepted")
    s = summary(conn, "SKU-103")
    assert (s["fbo_transit"], s["fbo_accepted"]) == (0, 3)


def test_shortage_on_chosen_ff_with_enough_in_total(conn):
    with pytest.raises(Shortage) as e:
        run(conn, stock.outbound, "fbs", SOUTH, "SKU-101", 8, uuid4())
    assert e.value.enough_total and e.value.available == 6
    assert [(o["name"], o["available"]) for o in e.value.others] == [("Север", 10)]
    # failed attempt left no trace: no document, no reserve
    assert conn.execute("SELECT count(*) AS n FROM docs WHERE kind = 'fbs'").fetchone()["n"] == 0
    assert at(conn, SOUTH, "SKU-101") == (6, 0)


def test_split_takes_chosen_ff_first_and_is_idempotent(conn):
    rid = uuid4()
    ids = run(conn, stock.outbound, "fbs", SOUTH, "SKU-101", 8, rid, True)
    assert len(ids) == 2
    assert at(conn, SOUTH, "SKU-101") == (6, 6) and at(conn, NORTH, "SKU-101") == (10, 2)
    assert run(conn, stock.outbound, "fbs", SOUTH, "SKU-101", 8, rid, True) == []
    assert at(conn, NORTH, "SKU-101") == (10, 2)
    summary(conn, "SKU-101")


def test_split_fails_when_total_is_not_enough(conn):
    with pytest.raises(StockError, match="доступно 4"):
        run(conn, stock.outbound, "fbo", NORTH, "SKU-103", 5, uuid4(), True)
    assert at(conn, SOUTH, "SKU-103") == (4, 0)


def test_delete_ff_with_stock_keeps_stock_and_history(conn):
    run(conn, stock.delete_warehouse, NORTH)
    assert at(conn, NORTH, "SKU-101") == (10, 0)
    rows = [r for r in stock.overview(conn)["by_wh"] if r["wh_id"] == NORTH]
    assert {r["sku"] for r in rows} == {"SKU-101", "SKU-102"} and all(r["deleted"] for r in rows)
    assert summary(conn, "SKU-101")["on_hand"] == 16
    assert any(h["action"] == "ФФ удалён" for h in stock.history(conn))
    assert any(h["action"] == "Приход" and h["dst"] == "ФФ Север" for h in stock.history(conn))

    # no new operations on a deleted ФФ...
    for fn, args in [(stock.receive, (NORTH, "SKU-101", 1, uuid4())),
                     (stock.outbound, ("fbs", NORTH, "SKU-101", 1, uuid4())),
                     (stock.transfer_send, (SOUTH, NORTH, "SKU-101", 1, uuid4()))]:
        with pytest.raises(StockError, match="удалён"):
            run(conn, fn, *args)
    # ...except moving the stock out of it
    doc = run(conn, stock.transfer_send, NORTH, SOUTH, "SKU-101", 10, uuid4())
    run(conn, stock.confirm, doc, "accept")
    assert at(conn, NORTH, "SKU-101") == (0, 0) and at(conn, SOUTH, "SKU-101") == (16, 0)
    summary(conn, "SKU-101")

    # the name is free again for a new ФФ
    assert run(conn, stock.add_warehouse, "Север") == 3


def test_delete_ff_with_open_operations_lets_them_finish(conn):
    [res] = run(conn, stock.outbound, "fbs", NORTH, "SKU-101", 2, uuid4())
    incoming = run(conn, stock.transfer_send, SOUTH, NORTH, "SKU-101", 3, uuid4())
    run(conn, stock.delete_warehouse, NORTH)
    assert run(conn, stock.confirm, res, "ship") == (True, "shipped")
    assert run(conn, stock.confirm, incoming, "accept") == (True, "received")
    assert at(conn, NORTH, "SKU-101") == (11, 0)
    summary(conn, "SKU-101")


def test_rename_and_duplicate_names(conn):
    run(conn, stock.rename_warehouse, NORTH, "Север-2")
    with pytest.raises(StockError, match="уже есть"):
        run(conn, stock.add_warehouse, "юг")
    with pytest.raises(StockError, match="уже есть"):
        run(conn, stock.rename_warehouse, NORTH, "Юг")


def test_concurrent_confirm_writes_off_once(conn):
    [doc] = run(conn, stock.outbound, "fbs", NORTH, "SKU-101", 4, uuid4())
    with db.connect() as c1, db.connect() as c2:
        assert stock.confirm(c1, doc, "ship") == (True, "shipped")  # holds the row lock, not committed
        result = {}
        t = threading.Thread(target=lambda: result.update(r=stock.confirm(c2, doc, "ship")))
        t.start()
        t.join(0.5)
        assert t.is_alive(), "second confirm must wait for the first one"
        c1.commit()
        t.join(5)
        c2.commit()
    assert result["r"] == (False, "shipped")
    assert at(conn, NORTH, "SKU-101") == (6, 0)


def test_web_pages_and_shortage_flow(conn):
    with TestClient(app) as client:
        for url in ["/", "/ops", "/warehouses", "/history"]:
            assert client.get(url).status_code == 200, url
        r = client.post("/ops/fbs", data={"wh": SOUTH, "sku": "SKU-101", "qty": 8, "request_id": str(uuid4())})
        assert r.status_code == 409 and "Разбить" in r.text and "Всё с ФФ «Север»" in r.text
        r = client.post("/ops/fbs", data={"wh": NORTH, "sku": "SKU-101", "qty": 1, "request_id": str(uuid4())})
        assert "Резерв FBS создан" in r.text
        doc = conn.execute("SELECT max(id) AS id FROM docs").fetchone()["id"]
        assert "отгружено" in client.post(f"/docs/{doc}/ship").text
        assert "уже обработан" in client.post(f"/docs/{doc}/ship").text
        assert client.post("/warehouses/1/delete").status_code == 200
        assert "удалён" in client.get("/").text
