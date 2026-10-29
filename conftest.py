import os

os.environ.setdefault("VERIFY_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest


@pytest.fixture(autouse=True)
def state_db_aislada(tmp_path, monkeypatch):
    """Cada prueba usa su propia base SQLite: el estado y la deduplicación no se mezclan entre pruebas."""
    from app.config import settings

    monkeypatch.setattr(settings, "state_db_path", str(tmp_path / "conversaciones.db"))


@pytest.fixture(autouse=True)
def dentro_del_horario(monkeypatch):
    """Por defecto las pruebas corren «dentro del horario de atención»; las de fuera de horario lo cambian."""
    monkeypatch.setattr("app.router.is_open", lambda now: True)


@pytest.fixture(autouse=True)
def bold_desactivado(monkeypatch):
    """Sin llaves de Bold por defecto, aunque el .env las tenga: ninguna prueba crea links reales."""
    from app.config import settings

    monkeypatch.setattr(settings, "bold_api_key", "")
    monkeypatch.setattr(settings, "bold_secret_key", "")
    monkeypatch.setattr(settings, "bold_sandbox", False)
    monkeypatch.setattr(settings, "bold_return_url", "")


@pytest.fixture(autouse=True)
def flow_del_kit_desactivado(monkeypatch):
    """Sin WhatsApp Flow por defecto, aunque el .env tenga WHATSAPP_KIT_FLOW_ID: el kit usa la lista de una en una."""
    from app.config import settings

    monkeypatch.setattr(settings, "whatsapp_kit_flow_id", "")
