import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def sin_firma_de_meta(monkeypatch):
    """Las pruebas envían avisos de WhatsApp sin firmar; las de la firma ponen su propia llave."""
    monkeypatch.setattr(settings, "app_secret", "")


@pytest.fixture(autouse=True)
def sin_enlaces_del_equipo(monkeypatch):
    """Sin dirección pública ni llave de enlaces por defecto, aunque el .env las tenga; las pruebas que las usan
    ponen las suyas."""
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "transfer_link_secret", "")
    monkeypatch.setattr(settings, "meta_app_id", "")
    monkeypatch.setattr(settings, "meta_signup_config_id", "")
