import os

import pytest

# Tests wipe data, so they use their own database. Must be set before `app` is imported.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://checkstock:checkstock@localhost:5433/checkstock_test"
)

from app import db, stock  # noqa: E402


@pytest.fixture
def conn():
    """Seeded DB; autocommit so every `run(...)` is a real committed transaction, as in the app."""
    with db.connect() as c:
        db.init_schema(c)
        stock.seed(c)
        c.commit()
        c.autocommit = True
        yield c
