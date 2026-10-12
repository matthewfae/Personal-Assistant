import pytest

import db.connection


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point every test at a fresh, empty database instead of the live one."""
    monkeypatch.setattr(db.connection, "_DB_PATH", str(tmp_path / "test.db"))
    db.connection.init_db()
