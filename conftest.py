import os

os.environ.setdefault("VERIFY_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest


@pytest.fixture(autouse=True)
def state_db_aislada(tmp_path, monkeypatch):
    """Cada prueba usa su propia base SQLite: el estado y la deduplicación no se mezclan entre pruebas."""
    from app.config import settings

    monkeypatch.setattr(settings, "state_db_path", str(tmp_path / "conversaciones.db"))
