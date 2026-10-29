"""Fase 1: estado de conversación, deduplicación y menú principal como lista de WhatsApp."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import state, whatsapp
from app.flows import EN_CONSTRUCCION, get_step, list_rows
from app.main import app
from app.state import Conversation, load_conversation, mark_processed, save_conversation

REMITENTE = "573001112233"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mocks(monkeypatch):
    m = {
        "ask": AsyncMock(return_value="respuesta de Claude"),
        "text": AsyncMock(return_value=True),
        "list": AsyncMock(return_value=True),
    }
    monkeypatch.setattr("app.router.ask_claude", m["ask"])
    monkeypatch.setattr("app.router.send_text", m["text"])
    monkeypatch.setattr("app.router.send_list", m["list"])
    m["buttons"] = AsyncMock(return_value=True)
    monkeypatch.setattr("app.router.send_buttons", m["buttons"])
    return m


def _payload(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "p1"},
            "contacts": [{"profile": {"name": "Laura"}, "wa_id": REMITENTE}],
            "messages": [{"from": REMITENTE, "timestamp": "1", **message}],
        }}]}],
    }


def texto(msg_id: str, body: str) -> dict:
    return _payload({"id": msg_id, "type": "text", "text": {"body": body}})


def eleccion_lista(msg_id: str, opcion_id: str, titulo: str = "x") -> dict:
    return _payload({"id": msg_id, "type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": opcion_id, "title": titulo},
    }})


# ── Almacén de estado ─────────────────────────────────────────────────────────

class TestEstado:
    def test_conversacion_nueva_vacia(self):
        conv = load_conversation(REMITENTE)
        assert conv.remitente == REMITENTE
        assert conv.flujo_activo is None and conv.historial == []

    def test_guardar_y_cargar(self):
        conv = Conversation(remitente=REMITENTE, flujo_activo="compra", paso="P1", datos={"ciudad": "Medellín"})
        conv.add_turn("hola", "¡Hola!")
        save_conversation(conv)
        cargada = load_conversation(REMITENTE)
        assert cargada.flujo_activo == "compra"
        assert cargada.datos == {"ciudad": "Medellín"}
        assert cargada.historial == [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "¡Hola!"},
        ]

    def test_conversacion_caducada_empieza_de_cero(self, monkeypatch):
        save_conversation(Conversation(remitente=REMITENTE, flujo_activo="compra"))
        ahora = time.time()
        monkeypatch.setattr(state.time, "time", lambda: ahora + state.CONVERSATION_TTL_SECONDS + 1)
        assert load_conversation(REMITENTE).flujo_activo is None

    def test_historial_limitado_y_empieza_por_cliente(self):
        conv = Conversation(remitente=REMITENTE)
        for i in range(state.MAX_HISTORIAL):
            conv.add_turn(f"u{i}", f"a{i}")
        assert len(conv.historial) == state.MAX_HISTORIAL
        assert conv.historial[0]["role"] == "user"
        assert conv.historial[-1]["content"] == f"a{state.MAX_HISTORIAL - 1}"

    def test_mark_processed_detecta_duplicados(self):
        assert mark_processed("wamid.1") is True
        assert mark_processed("wamid.1") is False
        assert mark_processed("wamid.2") is True


# ── Webhook ───────────────────────────────────────────────────────────────────

class TestDeduplicacion:
    def test_mismo_mensaje_dos_veces_responde_una(self, client, mocks):
        client.post("/webhook", json=texto("wamid.dup", "¿Cuál es el horario?"))
        client.post("/webhook", json=texto("wamid.dup", "¿Cuál es el horario?"))
        mocks["ask"].assert_called_once()
        mocks["list"].assert_called_once()  # primer mensaje: respuesta con el menú

    def test_eleccion_duplicada_responde_una(self, client, mocks):
        client.post("/webhook", json=eleccion_lista("wamid.ldup", "menu_comprar"))
        client.post("/webhook", json=eleccion_lista("wamid.ldup", "menu_comprar"))
        mocks["list"].assert_called_once()


class TestMenu:
    def test_menu_por_palabra_clave(self, client, mocks):
        save_conversation(Conversation(remitente=REMITENTE, flujo_activo="compra", paso="P1"))
        client.post("/webhook", json=texto("wamid.m1", "Menú"))
        cuerpo = mocks["list"].call_args.args[1]
        assert cuerpo == get_step("G1")["texto"]
        assert load_conversation(REMITENTE).flujo_activo is None

    def test_eleccion_del_menu_guarda_flujo_y_responde(self, client, mocks):
        client.post("/webhook", json=eleccion_lista("wamid.e1", "menu_proveedor", "🤝 Soy proveedor"))
        mocks["text"].assert_called_once_with(REMITENTE, EN_CONSTRUCCION.format(opcion="🤝 Soy proveedor"))
        conv = load_conversation(REMITENTE)
        assert (conv.flujo_activo, conv.paso) == ("proveedor", "V1")
        assert conv.historial[0] == {"role": "user", "content": "Elegí «🤝 Soy proveedor»"}

    def test_respuesta_de_boton_tambien_se_reconoce(self, client, mocks):
        payload = _payload({"id": "wamid.b1", "type": "interactive", "interactive": {
            "type": "button_reply", "button_reply": {"id": "menu_seguimiento", "title": "📦 Seguir mi pedido"},
        }})
        client.post("/webhook", json=payload)
        assert load_conversation(REMITENTE).flujo_activo == "seguimiento"

    def test_opcion_desconocida_reenvia_menu(self, client, mocks):
        client.post("/webhook", json=eleccion_lista("wamid.e2", "opcion_vieja"))
        mocks["list"].assert_called_once()
        mocks["text"].assert_not_called()
        assert load_conversation(REMITENTE).flujo_activo is None

    def test_interactivo_sin_respuesta_se_ignora(self, client, mocks):
        payload = _payload({"id": "wamid.n1", "type": "interactive", "interactive": {"type": "nfm_reply"}})
        assert client.post("/webhook", json=payload).status_code == 200
        mocks["text"].assert_not_called()
        mocks["list"].assert_not_called()

    def test_saludo_a_mitad_de_flujo_retoma_el_paso(self, client, mocks):
        client.post("/webhook", json=eleccion_lista("wamid.f1", "menu_comprar", "🧂 Comprar mezclas"))
        client.post("/webhook", json=texto("wamid.f2", "hola"))
        cuerpo, filas = mocks["list"].call_args.args[1], mocks["list"].call_args.args[3]
        assert cuerpo.startswith("¡Hola de nuevo! 🌿 Sigamos donde íbamos.") and "¿Qué quieres hacer?" in cuerpo
        assert filas[0]["id"] == "p1_catalogo"  # las opciones de P1, no el menú
        mocks["ask"].assert_not_called()


class TestInicioDeConversacion:
    def test_primer_mensaje_que_no_es_saludo_recibe_respuesta_con_menu(self, client, mocks):
        client.post("/webhook", json=texto("wamid.i1", "Quiero comprar unas mezclas"))
        destinatario, cuerpo, boton, filas = mocks["list"].call_args.args
        assert destinatario == REMITENTE and cuerpo == "respuesta de Claude" and boton == "Ver opciones"
        assert filas[0]["id"] == "menu_comprar"
        mocks["text"].assert_not_called()

    def test_segundo_mensaje_tambien_llega_con_opciones(self, client, mocks):
        client.post("/webhook", json=texto("wamid.i2", "¿Cuál es el horario?"))
        client.post("/webhook", json=texto("wamid.i3", "¿Y los domingos?"))
        assert mocks["list"].call_count == 2
        assert mocks["list"].call_args.args[1] == "respuesta de Claude"
        mocks["text"].assert_not_called()

    def test_respuesta_larga_va_aparte_del_menu(self, client, mocks):
        mocks["ask"].return_value = "x" * 1100
        client.post("/webhook", json=texto("wamid.i4", "Cuéntame todo sobre las mezclas"))
        mocks["text"].assert_called_once()
        assert mocks["list"].call_args.args[1] == get_step("G1")["texto"]


class TestHistorial:
    def test_claude_recibe_el_contexto_previo(self, client, mocks):
        client.post("/webhook", json=texto("wamid.h1", "Quiero sal de oro"))
        client.post("/webhook", json=texto("wamid.h2", "¿y cuánto cuesta?"))
        prompt, historial = mocks["ask"].call_args.args
        assert prompt == "¿y cuánto cuesta?"
        assert historial == [
            {"role": "user", "content": "Quiero sal de oro"},
            {"role": "assistant", "content": "respuesta de Claude"},
        ]

    def test_saludo_y_menu_quedan_en_el_historial(self, client, mocks):
        client.post("/webhook", json=texto("wamid.h3", "hola"))
        historial = load_conversation(REMITENTE).historial
        assert historial[0] == {"role": "user", "content": "hola"}
        assert "Laura" in historial[1]["content"]

    def test_envio_fallido_no_se_guarda_en_historial(self, client, mocks):
        mocks["list"].return_value = False
        client.post("/webhook", json=texto("wamid.h4", "¿Cuál es el horario?"))
        assert load_conversation(REMITENTE).historial == []


# ── Envío y lectura de mensajes interactivos ──────────────────────────────────

class TestWhatsAppInteractivo:
    @pytest.fixture
    def graph(self, monkeypatch):
        """Reemplaza Graph API por un transporte falso que registra lo enviado."""
        enviados = []
        respuesta = {"status": 200, "json": {"messages": [{"id": "wamid.out"}]}}

        def handler(request: httpx.Request) -> httpx.Response:
            enviados.append(json.loads(request.content))
            return httpx.Response(respuesta["status"], json=respuesta["json"])

        cliente_real = httpx.AsyncClient
        monkeypatch.setattr(whatsapp.httpx, "AsyncClient", lambda: cliente_real(transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(whatsapp.settings, "whatsapp_token", "token-prueba")
        monkeypatch.setattr(whatsapp.settings, "whatsapp_phone_number_id", "123")
        return enviados, respuesta

    def test_send_list_arma_el_payload_de_lista(self, graph):
        enviados, _ = graph
        ok = asyncio.run(whatsapp.send_list(REMITENTE, "¿Qué buscas?", "Ver opciones", list_rows("G1")))
        assert ok is True
        enviado = enviados[0]
        assert enviado["type"] == "interactive" and enviado["to"] == REMITENTE
        lista = enviado["interactive"]
        assert lista["type"] == "list"
        assert lista["body"] == {"text": "¿Qué buscas?"}
        assert lista["action"]["button"] == "Ver opciones"
        filas = lista["action"]["sections"][0]["rows"]
        assert len(filas) == 7
        assert filas[0] == {"id": "menu_comprar", "title": "🧂 Comprar mezclas", "description": "Catálogo, kits y pedidos"}

    def test_send_buttons_arma_el_payload_de_botones(self, graph):
        enviados, _ = graph
        botones = [{"id": "p9s_si", "title": "Sí"}, {"id": "p9s_no", "title": "No"}]
        assert asyncio.run(whatsapp.send_buttons(REMITENTE, "¿Te sirvió?", botones)) is True
        interactivo = enviados[0]["interactive"]
        assert interactivo["type"] == "button"
        assert interactivo["body"] == {"text": "¿Te sirvió?"}
        assert interactivo["action"]["buttons"] == [
            {"type": "reply", "reply": {"id": "p9s_si", "title": "Sí"}},
            {"type": "reply", "reply": {"id": "p9s_no", "title": "No"}},
        ]

    def test_envio_rechazado_devuelve_false(self, graph):
        _, respuesta = graph
        respuesta.update(status=401, json={"error": {"message": "Authentication Error", "code": 190}})
        assert asyncio.run(whatsapp.send_text(REMITENTE, "hola")) is False

    @pytest.mark.parametrize("interactive, esperado", [
        ({"type": "list_reply", "list_reply": {"id": "menu_regalo", "title": "🎁"}}, "menu_regalo"),
        ({"type": "button_reply", "button_reply": {"id": "p8_confirmar", "title": "Confirmar"}}, "p8_confirmar"),
        ({"type": "nfm_reply", "nfm_reply": {}}, None),
        ({"type": "list_reply", "list_reply": {}}, None),
        (None, None),
    ])
    def test_interactive_reply(self, interactive, esperado):
        reply = whatsapp.interactive_reply(interactive)
        assert (reply or {}).get("id") == esperado


def test_send_flow_arma_el_mensaje_de_formulario(monkeypatch):
    enviados = []

    def handler(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    cliente_real = httpx.AsyncClient
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", lambda: cliente_real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(whatsapp.settings, "whatsapp_token", "token-prueba")
    monkeypatch.setattr(whatsapp.settings, "whatsapp_phone_number_id", "123")
    asyncio.run(whatsapp.send_flow(REMITENTE, "Arma tu kit", "Elegir mezclas", "FLOW1", "kit-3", "KIT", {"cantidad": 3}, "draft"))
    parametros = enviados[0]["interactive"]["action"]["parameters"]
    assert enviados[0]["interactive"]["type"] == "flow"
    assert parametros == {
        "flow_message_version": "3", "flow_token": "kit-3", "flow_id": "FLOW1", "flow_cta": "Elegir mezclas",
        "flow_action": "navigate", "flow_action_payload": {"screen": "KIT", "data": {"cantidad": 3}}, "mode": "draft",
    }


def test_flow_response():
    assert whatsapp.flow_response({"type": "nfm_reply", "nfm_reply": {"response_json": '{"mezclas": ["a"]}'}}) == {"mezclas": ["a"]}
    assert whatsapp.flow_response({"type": "list_reply", "list_reply": {"id": "x"}}) is None
    assert whatsapp.flow_response({"type": "nfm_reply", "nfm_reply": {"response_json": "no es json"}}) is None
