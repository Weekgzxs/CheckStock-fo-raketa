import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://checkstock:checkstock@localhost:5433/checkstock"
)


def connect(url: str | None = None) -> psycopg.Connection:
    """`with connect() as conn:` commits on success and rolls back on any exception."""
    return psycopg.connect(url or DATABASE_URL, row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
