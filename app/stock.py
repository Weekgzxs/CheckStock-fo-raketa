"""Stock accounting. Every public function runs inside the caller's transaction.

Model:
  stock(on_hand, reserved)  - goods physically on a ФФ; available = on_hand - reserved
  docs(kind, status, qty)   - operations; goods in transit = qty of docs in a transit status
Invariant per SKU (shown on the main screen and checked in tests):
  received = on_hand + between ФФ + FBO in transit + at marketplace + FBS to client
"""

import uuid

from psycopg import Connection

STATUS = {
    "done": "проведён",
    "in_transit": "в пути",
    "received": "принято на ФФ",
    "reserved": "в резерве",
    "shipped": "отгружено клиенту",
    "cancelled": "отменён",
    "accepted": "принято складом МП",
}

# (doc kind, action) -> (required status, new status)
TRANSITIONS = {
    ("transfer", "accept"): ("in_transit", "received"),
    ("fbo", "accept"): ("in_transit", "accepted"),
    ("fbs", "ship"): ("reserved", "shipped"),
    ("fbs", "cancel"): ("reserved", "cancelled"),
}

SEED_PRODUCTS = [  # sku, name, receipt to "Север", receipt to "Юг"
    ("SKU-101", "Органайзер", 10, 6),
    ("SKU-102", "Контейнер", 5, 0),
    ("SKU-103", "Корзина", 0, 4),
]


class StockError(Exception):
    """A business-rule violation; the message is shown to the manager as is."""


class Shortage(StockError):
    def __init__(self, sku, wh, need, available, others):
        self.sku, self.wh, self.need, self.available, self.others = sku, wh, need, available, others
        self.total = available + sum(o["available"] for o in others)
        msg = f"На ФФ «{wh['name']}» доступно {available} шт. {sku}, нужно {need}."
        if self.enough_total:
            msg += " По всем ФФ хватает: " + ", ".join(
                f"{o['name']} — {o['available']}" for o in others
            ) + "."
        else:
            msg += f" По всем ФФ доступно только {self.total} шт."
        super().__init__(msg)

    @property
    def enough_total(self):
        return self.total >= self.need


# ---------- helpers ----------

def _wh(conn, wh_id, allow_deleted=False):
    w = conn.execute("SELECT * FROM warehouses WHERE id = %s", (wh_id,)).fetchone()
    if not w:
        raise StockError(f"ФФ #{wh_id} не найден")
    if w["deleted_at"] and not allow_deleted:
        raise StockError(
            f"ФФ «{w['name']}» удалён: новые операции с ним запрещены, остаток можно только переместить"
        )
    return w


def _check_input(conn, sku, qty):
    if qty <= 0:
        raise StockError("Количество должно быть больше нуля")
    if not conn.execute("SELECT 1 FROM products WHERE sku = %s", (sku,)).fetchone():
        raise StockError(f"Товар {sku} не найден")


def _add(conn, wh_id, sku, on_hand=0, reserved=0):
    # Not a delta upsert: Postgres checks CHECKs on the proposed INSERT row, and deltas can be negative.
    conn.execute("INSERT INTO stock (warehouse_id, sku) VALUES (%s, %s) ON CONFLICT DO NOTHING", (wh_id, sku))
    conn.execute(
        "UPDATE stock SET on_hand = on_hand + %s, reserved = reserved + %s WHERE warehouse_id = %s AND sku = %s",
        (on_hand, reserved, wh_id, sku),
    )


def _require_available(conn, w, sku, qty):
    """Lock the stock row and make sure `qty` is available, else raise Shortage with alternatives."""
    row = conn.execute(
        "SELECT on_hand - reserved AS available FROM stock WHERE warehouse_id = %s AND sku = %s FOR UPDATE",
        (w["id"], sku),
    ).fetchone()
    available = row["available"] if row else 0
    if available < qty:
        others = conn.execute(
            """SELECT w.id, w.name, s.on_hand - s.reserved AS available
               FROM stock s JOIN warehouses w ON w.id = s.warehouse_id
               WHERE s.sku = %s AND w.id <> %s AND w.deleted_at IS NULL AND s.on_hand > s.reserved
               ORDER BY available DESC, w.id""",
            (sku, w["id"]),
        ).fetchall()
        raise Shortage(sku, w, qty, available, others)


def _new_doc(conn, kind, status, sku, qty, from_wh, to_wh, request_id):
    """Create a document; returns None if this request_id was already processed (double submit)."""
    row = conn.execute(
        """INSERT INTO docs (kind, status, sku, qty, from_wh, to_wh, request_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (request_id) DO NOTHING RETURNING id""",
        (kind, status, sku, qty, from_wh, to_wh, request_id),
    ).fetchone()
    return row and row["id"]


def _log(conn, doc_id, action, sku, qty, src, dst):
    conn.execute(
        "INSERT INTO history (doc_id, action, sku, qty, src, dst) VALUES (%s, %s, %s, %s, %s, %s)",
        (doc_id, action, sku, qty, src, dst),
    )


# ---------- operations ----------

def receive(conn: Connection, wh_id, sku, qty, request_id):
    w = _wh(conn, wh_id)
    _check_input(conn, sku, qty)
    doc = _new_doc(conn, "receipt", "done", sku, qty, None, wh_id, request_id)
    if doc:
        _add(conn, wh_id, sku, on_hand=qty)
        _log(conn, doc, "Приход", sku, qty, "Поставщик", f"ФФ {w['name']}")
    return doc


def transfer_send(conn: Connection, from_id, to_id, sku, qty, request_id):
    if from_id == to_id:
        raise StockError("ФФ отправки и получения совпадают")
    src = _wh(conn, from_id, allow_deleted=True)  # moving stock out of a deleted ФФ is the way to empty it
    dst = _wh(conn, to_id)
    _check_input(conn, sku, qty)
    doc = _new_doc(conn, "transfer", "in_transit", sku, qty, from_id, to_id, request_id)
    if doc:
        _require_available(conn, src, sku, qty)
        _add(conn, from_id, sku, on_hand=-qty)
        _log(conn, doc, "Отправка перемещения", sku, qty, f"ФФ {src['name']}", f"В пути на ФФ {dst['name']}")
    return doc


def _outbound_one(conn, kind, wh_id, sku, qty, request_id):
    w = _wh(conn, wh_id)
    _check_input(conn, sku, qty)
    doc = _new_doc(conn, kind, "reserved" if kind == "fbs" else "in_transit", sku, qty, wh_id, None, request_id)
    if not doc:
        return None
    _require_available(conn, w, sku, qty)
    if kind == "fbs":  # FBS: goods stay on the shelf but are blocked until shipped or cancelled
        _add(conn, wh_id, sku, reserved=qty)
        _log(conn, doc, "Резерв FBS", sku, qty, f"ФФ {w['name']}: доступно", f"ФФ {w['name']}: резерв FBS")
    else:  # FBO: goods leave the ФФ right away and are in transit until the marketplace accepts them
        _add(conn, wh_id, sku, on_hand=-qty)
        _log(conn, doc, "Отгрузка FBO", sku, qty, f"ФФ {w['name']}", "В пути на склад МП (FBO)")
    return doc


def split_plan(conn, wh_id, sku, qty):
    """Take what the chosen ФФ has, then the rest from other ФФ with the most available."""
    rows = conn.execute(
        """SELECT s.warehouse_id, s.on_hand - s.reserved AS available
           FROM stock s JOIN warehouses w ON w.id = s.warehouse_id
           WHERE s.sku = %s AND w.deleted_at IS NULL AND s.on_hand > s.reserved
           ORDER BY s.warehouse_id = %s DESC, available DESC, s.warehouse_id
           FOR UPDATE OF s""",
        (sku, wh_id),
    ).fetchall()
    plan, left = [], qty
    for r in rows:
        take = min(left, r["available"])
        plan.append((r["warehouse_id"], take))
        left -= take
        if not left:
            return plan
    raise StockError(f"Недостаточно {sku}: по всем ФФ доступно {qty - left} шт., нужно {qty}")


def outbound(conn: Connection, kind, wh_id, sku, qty, request_id, split=False):
    """FBS reserve or FBO shipment. Returns created doc ids ([] if it was a repeated submit)."""
    assert kind in ("fbs", "fbo")
    if not split:
        doc = _outbound_one(conn, kind, wh_id, sku, qty, request_id)
        return [doc] if doc else []
    _wh(conn, wh_id)
    _check_input(conn, sku, qty)
    part_ids = lambda i: uuid.uuid5(request_id, str(i))  # noqa: E731 - stable keys for the parts
    # All parts are created in one transaction, so if part 0 exists the whole split was already done.
    if conn.execute("SELECT 1 FROM docs WHERE request_id = %s", (part_ids(0),)).fetchone():
        return []
    ids = []
    for i, (w, q) in enumerate(split_plan(conn, wh_id, sku, qty)):
        doc = _outbound_one(conn, kind, w, sku, q, part_ids(i))
        if doc is None:  # a concurrent identical request won the race
            return []
        ids.append(doc)
    return ids


def confirm(conn: Connection, doc_id, action):
    """Advance a document. Returns (changed, status). A repeated confirm changes nothing."""
    doc = conn.execute("SELECT kind, status FROM docs WHERE id = %s", (doc_id,)).fetchone()
    if not doc:
        raise StockError(f"Документ #{doc_id} не найден")
    key = (doc["kind"], action)
    if key not in TRANSITIONS:
        raise StockError(f"Действие недоступно для документа #{doc_id}")
    required, new = TRANSITIONS[key]
    # The status check and change are one atomic UPDATE: of two concurrent confirms only one matches.
    doc = conn.execute(
        "UPDATE docs SET status = %s, updated_at = now() WHERE id = %s AND status = %s RETURNING *",
        (new, doc_id, required),
    ).fetchone()
    if doc is None:
        current = conn.execute("SELECT status FROM docs WHERE id = %s", (doc_id,)).fetchone()["status"]
        return False, current
    sku, qty = doc["sku"], doc["qty"]
    if key == ("transfer", "accept"):
        src = _wh(conn, doc["from_wh"], allow_deleted=True)
        dst = _wh(conn, doc["to_wh"], allow_deleted=True)  # goods already travelling still arrive
        _add(conn, dst["id"], sku, on_hand=qty)
        _log(conn, doc_id, "Приёмка перемещения", sku, qty, f"В пути из ФФ {src['name']}", f"ФФ {dst['name']}")
    elif key == ("fbo", "accept"):
        _log(conn, doc_id, "Приёмка FBO складом МП", sku, qty, "В пути на склад МП (FBO)", "Склад маркетплейса")
    else:
        w = _wh(conn, doc["from_wh"], allow_deleted=True)
        if action == "ship":
            _add(conn, w["id"], sku, on_hand=-qty, reserved=-qty)
            _log(conn, doc_id, "Отгрузка FBS", sku, qty, f"ФФ {w['name']}: резерв FBS", "К клиенту (FBS)")
        else:
            _add(conn, w["id"], sku, reserved=-qty)
            _log(conn, doc_id, "Отмена резерва FBS", sku, qty, f"ФФ {w['name']}: резерв FBS", f"ФФ {w['name']}: доступно")
    return True, new


# ---------- warehouses ----------

def _check_name(conn, name, exclude_id=None):
    name = name.strip()
    if not name:
        raise StockError("Название ФФ не может быть пустым")
    if conn.execute(
        "SELECT 1 FROM warehouses WHERE lower(name) = lower(%s) AND deleted_at IS NULL AND id IS DISTINCT FROM %s",
        (name, exclude_id),
    ).fetchone():
        raise StockError(f"ФФ «{name}» уже есть")
    return name


def add_warehouse(conn: Connection, name):
    name = _check_name(conn, name)
    wh_id = conn.execute("INSERT INTO warehouses (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    _log(conn, None, "ФФ добавлен", None, None, None, f"ФФ {name}")
    return wh_id


def rename_warehouse(conn: Connection, wh_id, name):
    old = _wh(conn, wh_id)["name"]
    name = _check_name(conn, name, wh_id)
    conn.execute("UPDATE warehouses SET name = %s WHERE id = %s", (name, wh_id))
    _log(conn, None, "ФФ переименован", None, None, f"ФФ {old}", f"ФФ {name}")


def delete_warehouse(conn: Connection, wh_id):
    """Soft delete: the ФФ disappears from new operations, its stock and history stay visible."""
    w = _wh(conn, wh_id)
    conn.execute("UPDATE warehouses SET deleted_at = now() WHERE id = %s", (wh_id,))
    left = conn.execute(
        "SELECT COALESCE(sum(on_hand), 0) AS n FROM stock WHERE warehouse_id = %s", (wh_id,)
    ).fetchone()["n"]
    _log(conn, None, "ФФ удалён", None, left or None, f"ФФ {w['name']}",
         f"остаток {left} шт. числится на удалённом ФФ" if left else "остатка нет")


# ---------- read models ----------

def overview(conn: Connection):
    summary = conn.execute(
        """SELECT p.sku, p.name,
                  COALESCE(s.on_hand, 0) AS on_hand, COALESCE(s.reserved, 0) AS reserved,
                  COALESCE(d.received, 0) AS received, COALESCE(d.transit_ff, 0) AS transit_ff,
                  COALESCE(d.fbo_transit, 0) AS fbo_transit, COALESCE(d.fbo_accepted, 0) AS fbo_accepted,
                  COALESCE(d.fbs_shipped, 0) AS fbs_shipped
           FROM products p
           LEFT JOIN (SELECT sku, sum(on_hand) AS on_hand, sum(reserved) AS reserved
                      FROM stock GROUP BY sku) s USING (sku)
           LEFT JOIN (SELECT sku,
                        sum(qty) FILTER (WHERE kind = 'receipt') AS received,
                        sum(qty) FILTER (WHERE kind = 'transfer' AND status = 'in_transit') AS transit_ff,
                        sum(qty) FILTER (WHERE kind = 'fbo' AND status = 'in_transit') AS fbo_transit,
                        sum(qty) FILTER (WHERE kind = 'fbo' AND status = 'accepted') AS fbo_accepted,
                        sum(qty) FILTER (WHERE kind = 'fbs' AND status = 'shipped') AS fbs_shipped
                      FROM docs GROUP BY sku) d USING (sku)
           ORDER BY p.sku"""
    ).fetchall()
    for r in summary:
        r["available"] = r["on_hand"] - r["reserved"]
        r["balanced"] = r["received"] == (
            r["on_hand"] + r["transit_ff"] + r["fbo_transit"] + r["fbo_accepted"] + r["fbs_shipped"]
        )
    by_wh = conn.execute(
        """SELECT p.sku, p.name, w.id AS wh_id, w.name AS wh, w.deleted_at IS NOT NULL AS deleted,
                  COALESCE(s.on_hand, 0) AS on_hand, COALESCE(s.reserved, 0) AS reserved,
                  COALESCE(s.on_hand, 0) - COALESCE(s.reserved, 0) AS available
           FROM products p CROSS JOIN warehouses w
           LEFT JOIN stock s ON s.sku = p.sku AND s.warehouse_id = w.id
           WHERE w.deleted_at IS NULL OR s.on_hand > 0
           ORDER BY p.sku, w.deleted_at NULLS FIRST, w.id"""
    ).fetchall()
    open_docs = conn.execute(
        """SELECT d.*, fw.name AS from_name, fw.deleted_at IS NOT NULL AS from_deleted,
                  tw.name AS to_name, tw.deleted_at IS NOT NULL AS to_deleted
           FROM docs d
           LEFT JOIN warehouses fw ON fw.id = d.from_wh
           LEFT JOIN warehouses tw ON tw.id = d.to_wh
           WHERE d.status IN ('in_transit', 'reserved', 'shipped')
           ORDER BY d.id DESC"""
    ).fetchall()
    return {"summary": summary, "by_wh": by_wh, "open_docs": open_docs}


def warehouses(conn: Connection):
    return conn.execute(
        """SELECT w.*,
                  COALESCE(sum(s.on_hand), 0) AS on_hand, COALESCE(sum(s.reserved), 0) AS reserved,
                  string_agg(s.sku || ': ' || s.on_hand, ', ' ORDER BY s.sku) FILTER (WHERE s.on_hand > 0) AS items,
                  (SELECT COALESCE(sum(qty), 0) FROM docs
                   WHERE to_wh = w.id AND kind = 'transfer' AND status = 'in_transit') AS incoming
           FROM warehouses w LEFT JOIN stock s ON s.warehouse_id = w.id
           GROUP BY w.id ORDER BY w.deleted_at NULLS FIRST, w.id"""
    ).fetchall()


def choices(conn: Connection):
    return {
        "products": conn.execute("SELECT * FROM products ORDER BY sku").fetchall(),
        "warehouses": warehouses(conn),
    }


def history(conn: Connection, limit=500):
    return conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


def seed(conn: Connection):
    """Reset to the test data from the assignment: one shop, ФФ Север and Юг, test receipts."""
    conn.execute("TRUNCATE history, docs, stock, products, warehouses RESTART IDENTITY CASCADE")
    north, south = add_warehouse(conn, "Север"), add_warehouse(conn, "Юг")
    for sku, name, n, s in SEED_PRODUCTS:
        conn.execute("INSERT INTO products (sku, name) VALUES (%s, %s)", (sku, name))
        for wh_id, qty in ((north, n), (south, s)):
            if qty:
                receive(conn, wh_id, sku, qty, uuid.uuid4())
