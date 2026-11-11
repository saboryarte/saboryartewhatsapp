import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def sin_firma_de_meta(monkeypatch):
    """Las pruebas envían avisos de WhatsApp sin firmar; las de la firma ponen su propia llave."""
    monkeypatch.setattr(settings, "app_secret", "")
