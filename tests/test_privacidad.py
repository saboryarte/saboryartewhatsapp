"""Página pública de la política de privacidad."""
from fastapi.testclient import TestClient

from app import privacidad
from app.main import app


def test_la_politica_se_publica_en_html():
    r = TestClient(app).get("/privacidad.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "<h1>Política de tratamiento de datos personales de Saboryarte</h1>" in r.text
    assert "Ley 1581 de 2012" in r.text
    assert "BORRADOR" not in r.text  # los comentarios para quien edita no se publican
    assert "<table>" in r.text and "<blockquote>" in r.text
    assert "[" not in r.text.split("<main>")[1]  # sin espacios por llenar del borrador
    assert "https://especia.mariomora.tech/privacidad.html" in r.text


def test_la_direccion_sin_extension_lleva_a_la_nueva():
    r = TestClient(app).get("/privacidad", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/privacidad.html"


def test_convierte_lo_basico_de_markdown_y_escapa_el_html():
    html = privacidad.to_html("# T\n\nUno **dos** *tres* <script>\n\n- a\n  b\n- c\n\n| x | y |\n|---|---|\n| 1 | 2 |")
    assert "<h1>T</h1>" in html and "<b>dos</b>" in html and "<i>tres</i>" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "<li>a b</li><li>c</li>" in html and "<td>1</td><td>2</td>" in html


# ── Aviso de privacidad por WhatsApp ──────────────────────────────────────────

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app import archivo

_send_notice = privacidad.send_notice  # el original; conftest lo apaga para las demás pruebas
CLIENTE = "573005556677"


@pytest.fixture
def envio(monkeypatch):
    monkeypatch.setattr(privacidad, "send_notice", _send_notice)
    m = AsyncMock(return_value=True)
    monkeypatch.setattr("app.privacidad.send_text", m)
    return m


def test_el_aviso_sale_de_la_politica_con_formato_de_whatsapp():
    aviso = privacidad.notice_text()
    assert aviso.startswith("Te cuento que Especia es nuestra asistente automática")
    assert "política de privacidad: https://especia.mariomora.tech/privacidad.html." in aviso
    assert "escribe *asesor*" in aviso
    assert "**" not in aviso and "\n" not in aviso and "[" not in aviso


def test_el_aviso_se_envia_una_sola_vez(envio):
    assert asyncio.run(privacidad.send_notice(CLIENTE)) is True
    assert asyncio.run(privacidad.send_notice(CLIENTE)) is False
    envio.assert_awaited_once_with(CLIENTE, privacidad.notice_text())


def test_si_no_se_pudo_enviar_se_intenta_en_el_proximo_mensaje(envio):
    envio.return_value = False
    assert asyncio.run(privacidad.send_notice(CLIENTE)) is False
    envio.side_effect = RuntimeError("sin red")
    assert asyncio.run(privacidad.send_notice(CLIENTE)) is False
    envio.side_effect, envio.return_value = None, True
    assert asyncio.run(privacidad.send_notice(CLIENTE)) is True


def test_despues_de_60_dias_sin_escribir_se_muestra_otra_vez(envio):
    asyncio.run(privacidad.send_notice(CLIENTE))
    archivo.purge(now=time.time() + 59 * archivo.DIA)
    assert not archivo.privacy_notice_pending(CLIENTE)
    archivo.purge(now=time.time() + 61 * archivo.DIA)
    assert archivo.privacy_notice_pending(CLIENTE)


def test_escribir_mantiene_vigente_el_aviso(envio, monkeypatch):
    asyncio.run(privacidad.send_notice(CLIENTE))
    hoy = time.time()
    with monkeypatch.context() as m:  # no usar monkeypatch.undo(): desharía también la base aislada de la prueba
        m.setattr(archivo.time, "time", lambda: hoy + 50 * archivo.DIA)
        archivo.touch_client(CLIENTE)
    archivo.purge(now=hoy + 100 * archivo.DIA)
    assert not archivo.privacy_notice_pending(CLIENTE)


def test_el_primer_mensaje_recibe_el_aviso_antes_de_la_respuesta(envio, monkeypatch):
    orden = []
    envio.side_effect = lambda to, text: orden.append("aviso") or True
    monkeypatch.setattr("app.router.send_list", AsyncMock(side_effect=lambda *a, **k: orden.append("menú") or True))
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "p1"},
        "contacts": [{"profile": {"name": "Laura"}, "wa_id": CLIENTE}],
        "messages": [{"from": CLIENTE, "timestamp": "1", "id": "", "type": "text", "text": {"body": "hola"}}],
    }}]}]}
    cliente = TestClient(app)
    for n in (1, 2):
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = f"wamid.aviso{n}"
        assert cliente.post("/webhook", json=payload).status_code == 200
    assert orden == ["aviso", "menú", "menú"]
