"""Pago con link de Bold: creación del link al confirmar, firma del webhook y avisos de pago."""
import asyncio
import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import bold
from app.claude import EnvioExtraido, LineaPedido, PedidoExtraido
from app.config import settings
from app.main import app
from app.pedidos import PAGADO, PENDIENTE, REVISAR, find_by_link, get_order
from app.state import load_conversation

REMITENTE = "573001112233"
LINK = {"id": "LNK_PRUEBA123", "url": "https://checkout.bold.co/LNK_PRUEBA123", "vence_en": 1_900_000_000.0}


def firmar(body: bytes, llave: str = "") -> str:
    return hmac.new(llave.encode(), base64.b64encode(body), hashlib.sha256).hexdigest()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mocks(monkeypatch):
    m = {name: AsyncMock(return_value=True) for name in ("text", "list", "buttons", "notify")}
    m["ask"] = AsyncMock(return_value="respuesta de Claude")
    for name, attr in [("ask", "ask_claude"), ("text", "send_text"), ("list", "send_list"),
                       ("buttons", "send_buttons"), ("notify", "notify_team")]:
        monkeypatch.setattr(f"app.router.{attr}", m[name])
    monkeypatch.setattr("app.router.extract_order", AsyncMock(return_value=PedidoExtraido(
        lineas=[LineaPedido(producto="sal_de_oro", cantidad=2)], no_reconocidos=[],
        quiere_distribuir=False, pregunta_o_comentario=False)))
    monkeypatch.setattr("app.router.extract_shipping", AsyncMock(return_value=EnvioExtraido(
        nombre="Laura Gómez", direccion="Cra 43 # 10-20", ciudad="Envigado", telefono="3001112233",
        pregunta_o_comentario=False)))
    m["crear_link"] = AsyncMock(return_value=LINK)
    m["estado_link"] = AsyncMock(return_value="PAID")
    monkeypatch.setattr("app.bold.create_payment_link", m["crear_link"])
    monkeypatch.setattr("app.bold.link_status", m["estado_link"])
    return m


@pytest.fixture
def bold_pruebas(monkeypatch):
    monkeypatch.setattr(settings, "bold_api_key", "llave-identidad-prueba")
    monkeypatch.setattr(settings, "bold_sandbox", True)


_contador = iter(range(10_000))


def _mensaje(message: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "p1"},
        "contacts": [{"profile": {"name": "Laura"}, "wa_id": REMITENTE}],
        "messages": [{"from": REMITENTE, "timestamp": "1", "id": f"wamid.b{next(_contador)}", **message}],
    }}]}]}


def elegir(client, opcion_id: str, tipo: str = "button_reply") -> None:
    client.post("/webhook", json=_mensaje({"type": "interactive", "interactive": {
        "type": tipo, tipo: {"id": opcion_id, "title": opcion_id}}}))


def escribir(client, body: str) -> None:
    client.post("/webhook", json=_mensaje({"type": "text", "text": {"body": body}}))


def armar_y_confirmar(client) -> None:
    elegir(client, "p1_pedido", "list_reply")
    escribir(client, "2 de oro")
    escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
    elegir(client, "p8_confirmar")


def aviso_bold(client, tipo: str, total: int = 64_000, link_id: str = LINK["id"], evento: str | None = None,
               llave: str = "") -> httpx.Response:
    cuerpo = json.dumps({
        "id": evento or f"evt-{next(_contador)}", "type": tipo, "subject": "PAY123", "source": "/payments/links",
        "data": {"payment_id": "PAY123", "amount": {"currency": "COP", "total": total, "tip": 0},
                 "metadata": {"reference": link_id}, "integration": "LINK", "payment_method": "PSE"},
    }).encode()
    return client.post("/webhooks/bold", content=cuerpo, headers={
        "content-type": "application/json", "x-bold-signature": firmar(cuerpo, llave)})


# ── Firma del webhook ─────────────────────────────────────────────────────────

class TestFirma:
    def test_pruebas_firma_con_llave_vacia(self, monkeypatch):
        monkeypatch.setattr(settings, "bold_sandbox", True)
        assert bold.valid_signature(b'{"a":1}', firmar(b'{"a":1}'))

    def test_produccion_exige_la_llave_secreta(self, monkeypatch):
        monkeypatch.setattr(settings, "bold_secret_key", "secreta-produccion")
        cuerpo = b'{"a":1}'
        assert bold.valid_signature(cuerpo, firmar(cuerpo, "secreta-produccion"))
        assert not bold.valid_signature(cuerpo, firmar(cuerpo))  # una firma con llave vacía no sirve
        assert not bold.valid_signature(b'{"a":2}', firmar(cuerpo, "secreta-produccion"))  # cuerpo alterado

    def test_produccion_sin_llave_rechaza_todo(self):
        assert not bold.valid_signature(b"{}", firmar(b"{}"))

    def test_sin_firma(self, monkeypatch):
        monkeypatch.setattr(settings, "bold_sandbox", True)
        assert not bold.valid_signature(b"{}", None)

    def test_endpoint_rechaza_firma_invalida(self, client, mocks, bold_pruebas):
        respuesta = client.post("/webhooks/bold", content=b'{"type":"SALE_APPROVED"}',
                                headers={"x-bold-signature": "firma-falsa"})
        assert respuesta.status_code == 400


# ── Confirmar pedido → link de pago ───────────────────────────────────────────

class TestLinkDePago:
    def test_confirmar_envia_link_por_el_total(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        total, descripcion, referencia = mocks["crear_link"].call_args.args
        assert total == 64_000  # 2 × $26.000 + envío a Envigado $12.000
        assert descripcion == "Pedido Saboryarte SAB-00001"
        assert referencia.startswith("SAB-00001-")
        texto = mocks["text"].call_args.args[1]
        assert "SAB-00001" in texto and "$64.000" in texto and LINK["url"] in texto and "24 horas" in texto
        pedido = find_by_link(LINK["id"])
        assert pedido.estado == PENDIENTE and pedido.envio["ciudad"] == "Envigado"
        conv = load_conversation(REMITENTE)
        assert not conv.en_traspaso and conv.paso == "P11L"
        mocks["notify"].assert_not_called()

    def test_confirmar_dos_veces_reenvia_el_mismo_link(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        elegir(client, "p8_confirmar")
        mocks["crear_link"].assert_called_once()
        assert LINK["url"] in mocks["text"].call_args.args[1]

    def test_si_bold_falla_pasa_a_una_persona(self, client, mocks, bold_pruebas):
        mocks["crear_link"].return_value = None
        armar_y_confirmar(client)
        assert mocks["text"].call_args.args[1].startswith("¡Pedido recibido! 🙌 Una persona")
        assert load_conversation(REMITENTE).en_traspaso
        assert get_order(1).estado == REVISAR

    def test_sin_bold_configurado_pasa_a_una_persona(self, client, mocks):
        armar_y_confirmar(client)
        mocks["crear_link"].assert_not_called()
        assert load_conversation(REMITENTE).en_traspaso


# ── Avisos de Bold ────────────────────────────────────────────────────────────

class TestAvisos:
    @pytest.fixture
    def pedido_con_link(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        mocks["text"].reset_mock()

    def test_pago_aprobado(self, client, mocks, pedido_con_link):
        assert aviso_bold(client, "SALE_APPROVED").status_code == 200
        pedido = get_order(1)
        assert pedido.estado == PAGADO and pedido.payment_id == "PAY123" and pedido.pagado_en
        destinatario, texto = mocks["text"].call_args.args
        assert destinatario == REMITENTE and texto.startswith("¡Recibimos tu pago!") and "SAB-00001" in texto
        asunto, ficha = mocks["notify"].call_args.args
        assert "Pedido pagado SAB-00001" in asunto and "despachar" in asunto
        assert "Envío a: Laura Gómez, Cra 43 # 10-20, Envigado" in ficha and "Pago Bold: PAY123" in ficha

    def test_aviso_repetido_se_procesa_una_vez(self, client, mocks, pedido_con_link):
        aviso_bold(client, "SALE_APPROVED", evento="evt-repetido")
        aviso_bold(client, "SALE_APPROVED", evento="evt-repetido")
        aviso_bold(client, "SALE_APPROVED")  # otro aviso del mismo pago: el pedido ya está pagado
        mocks["text"].assert_called_once()
        mocks["notify"].assert_called_once()

    def test_monto_distinto_queda_por_revisar(self, client, mocks, pedido_con_link):
        aviso_bold(client, "SALE_APPROVED", total=1_000)
        assert get_order(1).estado == REVISAR
        mocks["text"].assert_not_called()
        assert "Revisar pago" in mocks["notify"].call_args.args[0]

    def test_produccion_link_no_pagado_queda_por_revisar(self, client, mocks, pedido_con_link, monkeypatch):
        monkeypatch.setattr(settings, "bold_sandbox", False)
        monkeypatch.setattr(settings, "bold_secret_key", "secreta-produccion")
        mocks["estado_link"].return_value = "ACTIVE"
        aviso_bold(client, "SALE_APPROVED", llave="secreta-produccion")
        assert get_order(1).estado == REVISAR
        mocks["text"].assert_not_called()

    def test_produccion_espera_si_el_link_sigue_procesando(self, client, mocks, pedido_con_link, monkeypatch):
        monkeypatch.setattr(settings, "bold_sandbox", False)
        monkeypatch.setattr(settings, "bold_secret_key", "secreta-produccion")
        monkeypatch.setattr("app.bold.asyncio.sleep", AsyncMock())
        mocks["estado_link"].side_effect = ["PROCESSING", "PROCESSING", "PAID"]
        aviso_bold(client, "SALE_APPROVED", llave="secreta-produccion")
        assert get_order(1).estado == PAGADO
        assert mocks["estado_link"].call_count == 3

    def test_pruebas_no_exige_link_pagado(self, client, mocks, pedido_con_link):
        mocks["estado_link"].return_value = "PROCESSING"  # los pagos de prueba no cierran el link
        aviso_bold(client, "SALE_APPROVED")
        assert get_order(1).estado == PAGADO
        mocks["estado_link"].assert_not_called()

    def test_reconoce_la_referencia_enviada_al_crear_el_link(self, client, mocks, pedido_con_link):
        referencia = mocks["crear_link"].call_args.args[2]  # «SAB-00001-<timestamp>»
        aviso_bold(client, "SALE_APPROVED", link_id=referencia)
        assert get_order(1).estado == PAGADO

    def test_referencia_de_un_pedido_viejo_con_el_mismo_numero_se_ignora(self, client, mocks, pedido_con_link):
        aviso_bold(client, "SALE_APPROVED", link_id="SAB-00001-1700000000")  # otra marca de tiempo
        assert get_order(1).estado == PENDIENTE
        mocks["text"].assert_not_called()

    def test_pago_rechazado_reenvia_el_link(self, client, mocks, pedido_con_link):
        aviso_bold(client, "SALE_REJECTED")
        texto = mocks["text"].call_args.args[1]
        assert "no se pudo completar" in texto and LINK["url"] in texto
        assert get_order(1).estado == PENDIENTE

    def test_anulacion(self, client, mocks, pedido_con_link):
        aviso_bold(client, "VOID_APPROVED")
        assert get_order(1).estado == "anulado"
        assert "Pago anulado" in mocks["notify"].call_args.args[0]

    def test_aviso_de_otro_link_se_ignora(self, client, mocks, pedido_con_link):
        assert aviso_bold(client, "SALE_APPROVED", link_id="LNK_DESCONOCIDO").status_code == 200
        mocks["text"].assert_not_called()
        mocks["notify"].assert_not_called()


# ── Cliente HTTP de Bold ──────────────────────────────────────────────────────

class TestClienteBold:
    @pytest.fixture
    def api(self, monkeypatch):
        """Reemplaza la API de Bold por un transporte falso que registra las peticiones."""
        peticiones = []
        respuesta = {"status": 200, "json": {"payload": {"payment_link": "LNK_ABC", "url": "https://checkout.bold.co/LNK_ABC"}, "errors": []}}

        def handler(request: httpx.Request) -> httpx.Response:
            peticiones.append(request)
            return httpx.Response(respuesta["status"], json=respuesta["json"])

        cliente_real = httpx.AsyncClient
        monkeypatch.setattr(bold.httpx, "AsyncClient",
                            lambda **kw: cliente_real(transport=httpx.MockTransport(handler), **kw))
        monkeypatch.setattr(settings, "bold_api_key", "llave-identidad-prueba")
        return peticiones, respuesta

    def test_crea_link_de_monto_cerrado(self, api):
        peticiones, _ = api
        link = asyncio.run(bold.create_payment_link(64_000, "Pedido Saboryarte SAB-00001", "SAB-00001-1761"))
        assert link["id"] == "LNK_ABC" and link["url"] == "https://checkout.bold.co/LNK_ABC"
        peticion = peticiones[0]
        assert str(peticion.url) == "https://integrations.api.bold.co/online/link/v1"
        assert peticion.headers["Authorization"] == "x-api-key llave-identidad-prueba"
        cuerpo = json.loads(peticion.content)
        assert cuerpo["amount_type"] == "CLOSE"
        assert cuerpo["amount"] == {"currency": "COP", "total_amount": 64_000, "tip_amount": 0}
        assert "payment_methods" not in cuerpo  # todos los métodos habilitados
        assert cuerpo["expiration_date"] > 10**18  # nanosegundos Unix (Bold rechaza milisegundos)

    def test_error_de_bold_devuelve_none(self, api):
        _, respuesta = api
        respuesta.update(status=401, json={"errors": ["unauthorized"]})
        assert asyncio.run(bold.create_payment_link(64_000, "Pedido", "SAB-1")) is None

    def test_estado_del_link(self, api):
        _, respuesta = api
        respuesta.update(json={"payload": {"status": "PAID"}})
        assert asyncio.run(bold.link_status("LNK_ABC")) == "PAID"


def test_link_con_url_de_regreso(monkeypatch):
    """Con BOLD_RETURN_URL, el botón «Volver a la tienda» del comprobante lleva al chat de WhatsApp."""
    enviados = []

    def handler(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        return httpx.Response(200, json={"payload": {"payment_link": "LNK_X", "url": "https://checkout.bold.co/LNK_X"}, "errors": []})

    cliente_real = httpx.AsyncClient
    monkeypatch.setattr(bold.httpx, "AsyncClient", lambda **kw: cliente_real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "bold_api_key", "llave-identidad-prueba")
    monkeypatch.setattr(settings, "bold_return_url", "https://wa.me/15552020058")
    asyncio.run(bold.create_payment_link(26_000, "Pedido", "SAB-00002-1"))
    assert enviados[0]["callback_url"] == "https://wa.me/15552020058"
    monkeypatch.setattr(settings, "bold_return_url", "")
    asyncio.run(bold.create_payment_link(26_000, "Pedido", "SAB-00003-1"))
    assert "callback_url" not in enviados[1]
