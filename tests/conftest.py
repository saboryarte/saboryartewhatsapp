import pytest

from app import privacidad
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


async def _sin_aviso(remitente: str) -> bool:
    return False


@pytest.fixture(autouse=True)
def sin_aviso_de_privacidad(monkeypatch):
    """El aviso de privacidad sale una vez por cliente; las pruebas que cuentan mensajes no lo esperan.
    tests/test_privacidad.py lo vuelve a activar."""
    monkeypatch.setattr(privacidad, "send_notice", _sin_aviso)
