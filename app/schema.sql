-- Fulfilment warehouses (ФФ). Deletion is soft: stock and history must survive it.
CREATE TABLE IF NOT EXISTS warehouses (
    id         serial PRIMARY KEY,
    name       text NOT NULL CHECK (btrim(name) <> ''),
    deleted_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS warehouses_active_name
    ON warehouses (lower(name)) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS products (
    sku  text PRIMARY KEY,
    name text NOT NULL
);

-- What physically sits on a warehouse. available = on_hand - reserved.
-- The CHECKs are the last line of defence: stock can never go negative or be over-reserved.
CREATE TABLE IF NOT EXISTS stock (
    warehouse_id int  NOT NULL REFERENCES warehouses,
    sku          text NOT NULL REFERENCES products,
    on_hand      int  NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reserved     int  NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    PRIMARY KEY (warehouse_id, sku),
    CHECK (reserved <= on_hand)
);

-- Every operation is a document with a status. Goods "in transit" are not stored anywhere
-- else: they are the qty of documents in an in-transit status, so the numbers cannot drift.
CREATE TABLE IF NOT EXISTS docs (
    id         serial PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('receipt', 'transfer', 'fbs', 'fbo')),
    status     text NOT NULL CHECK (status IN ('done', 'in_transit', 'received', 'reserved',
                                               'shipped', 'cancelled', 'accepted')),
    sku        text NOT NULL REFERENCES products,
    qty        int  NOT NULL CHECK (qty > 0),
    from_wh    int REFERENCES warehouses,
    to_wh      int REFERENCES warehouses,
    request_id uuid NOT NULL UNIQUE,  -- idempotency key from the form: a double submit creates nothing
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (kind <> 'receipt'  OR (to_wh IS NOT NULL AND from_wh IS NULL)),
    CHECK (kind <> 'transfer' OR (from_wh IS NOT NULL AND to_wh IS NOT NULL AND from_wh <> to_wh)),
    CHECK (kind NOT IN ('fbs', 'fbo') OR (from_wh IS NOT NULL AND to_wh IS NULL))
);

-- Human-readable journal: what, how many, from where, to where. Places are name snapshots.
CREATE TABLE IF NOT EXISTS history (
    id     bigserial PRIMARY KEY,
    doc_id int REFERENCES docs,
    action text NOT NULL,
    sku    text REFERENCES products,
    qty    int,
    src    text,
    dst    text,
    at     timestamptz NOT NULL DEFAULT now()
);
