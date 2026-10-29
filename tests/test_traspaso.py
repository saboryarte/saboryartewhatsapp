"""Traspaso a una persona en modo coexistencia: disparadores, ficha, silencio, reactivación y alertas."""
import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import router
from app.claude import EnvioExtraido, LineaPedido, PedidoExtraido
from app.config import settings
from app.greetings import is_human_request
from app.handoff import COLOMBIA, business_minutes_between, handoff_ficha, is_closing_message, is_open
from app.main import app
from app.state import Conversation, load_conversation, save_conversation

REMITENTE = "573001112233"
NEGOCIO = "15550001111"


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
    return m


def _payload(field: str, value: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "e1", "changes": [{"field": field, "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "p1"}, **value,
    }}]}]}


_contador = iter(range(10_000))


def escribir(client, body: str) -> None:
    client.post("/webhook", json=_payload("messages", {
        "contacts": [{"profile": {"name": "Laura"}, "wa_id": REMITENTE}],
        "messages": [{"from": REMITENTE, "id": f"wamid.t{next(_contador)}", "timestamp": "1", "type": "text", "text": {"body": body}}],
    }))


def elegir(client, opcion_id: str, tipo: str = "list_reply") -> None:
    client.post("/webhook", json=_payload("messages", {
        "contacts": [{"profile": {"name": "Laura"}, "wa_id": REMITENTE}],
        "messages": [{"from": REMITENTE, "id": f"wamid.i{next(_contador)}", "timestamp": "1", "type": "interactive",
                      "interactive": {"type": tipo, tipo: {"id": opcion_id, "title": opcion_id}}}],
    }))


def equipo_escribe(client, body: str) -> None:
    """Eco de coexistencia: mensaje que el equipo envió desde la app de WhatsApp Business."""
    client.post("/webhook", json=_payload("smb_message_echoes", {
        "message_echoes": [{"from": NEGOCIO, "to": REMITENTE, "id": f"wamid.e{next(_contador)}", "timestamp": "1",
                            "type": "text", "text": {"body": body}}],
    }))


# ── Detección ─────────────────────────────────────────────────────────────────

class TestDeteccion:
    @pytest.mark.parametrize("texto", [
        "asesor", "Quiero hablar con un asesor", "necesito hablar con alguien", "¿me comunicas con una asesora?",
        "quiero hablar con una persona", "Pásame a un humano", "alguien del equipo por favor",
    ])
    def test_pide_persona(self, texto):
        assert is_human_request(texto)

    @pytest.mark.parametrize("texto", [
        "quiero una mezcla personalizada", "es un regalo para una persona especial", "hola", "2 de oro",
    ])
    def test_no_confunde_con_persona(self, texto):
        assert not is_human_request(texto)

    def test_frase_de_cierre(self):
        assert is_closing_message("¡Fue un gusto ayudarte! Te dejo de nuevo con Chatsabor 🌿")
        assert is_closing_message("¡Fue un gusto ayudarte! Te dejo de nuevo con nuestro asistente virtual 🌿")  # frase anterior
        assert not is_closing_message("Hola Laura, ya reviso tu pedido")


class TestHorario:
    @pytest.mark.parametrize("momento, abierto", [
        (datetime(2026, 10, 21, 10, 0, tzinfo=COLOMBIA), True),   # miércoles
        (datetime(2026, 10, 21, 18, 0, tzinfo=COLOMBIA), False),  # cierra a las 6 p. m.
        (datetime(2026, 10, 24, 9, 0, tzinfo=COLOMBIA), True),    # sábado
        (datetime(2026, 10, 25, 10, 0, tzinfo=COLOMBIA), False),  # domingo
    ])
    def test_is_open(self, momento, abierto):
        assert is_open(momento) is abierto

    def test_minutos_habiles_saltan_la_noche_y_el_domingo(self):
        sabado_tarde = datetime(2026, 10, 24, 17, 30, tzinfo=COLOMBIA)
        lunes_mañana = datetime(2026, 10, 26, 8, 45, tzinfo=COLOMBIA)
        assert business_minutes_between(sabado_tarde, lunes_mañana) == 75


# ── Disparadores y silencio ───────────────────────────────────────────────────

class TestTraspaso:
    def test_pedir_asesor_avisa_al_cliente_y_al_equipo(self, client, mocks):
        escribir(client, "quiero hablar con un asesor")
        assert mocks["text"].call_args.args[1].startswith("Te paso con Juanita")
        asunto, ficha = mocks["notify"].call_args.args
        assert "Pidió hablar con una persona" in asunto and "Laura" in asunto
        assert f"WhatsApp {REMITENTE}" in ficha
        conv = load_conversation(REMITENTE)
        assert conv.en_traspaso and conv.traspaso["motivo"] == "solicitud"

    def test_fuera_de_horario_avisa_cuando_abren(self, client, mocks, monkeypatch):
        monkeypatch.setattr("app.router.is_open", lambda now: False)
        escribir(client, "asesor")
        assert "fuera de horario" in mocks["text"].call_args.args[1]

    def test_fuera_de_horario_el_saludo_muestra_g2(self, client, mocks, monkeypatch):
        monkeypatch.setattr("app.router.is_open", lambda now: False)
        escribir(client, "hola")
        assert "fuera de horario" in mocks["list"].call_args.args[1]
        assert len(mocks["list"].call_args.args[3]) == 7

    def test_bot_en_silencio_mientras_atiende_una_persona(self, client, mocks):
        escribir(client, "asesor")
        mocks["text"].reset_mock()
        escribir(client, "hola, ¿sigues ahí?")
        elegir(client, "menu_comprar")
        mocks["text"].assert_not_called()
        mocks["list"].assert_not_called()
        mocks["ask"].assert_not_called()

    def test_menu_es_la_salida_del_cliente(self, client, mocks):
        escribir(client, "asesor")
        escribir(client, "menú")
        mocks["list"].assert_called_once()
        assert not load_conversation(REMITENTE).en_traspaso

    def test_ficha_del_pedido_incluye_items_total_y_envio(self, client, mocks, monkeypatch):
        monkeypatch.setattr("app.router.extract_order", AsyncMock(return_value=PedidoExtraido(
            lineas=[LineaPedido(producto="sal_de_oro", cantidad=2)], no_reconocidos=[],
            quiere_distribuir=False, pregunta_o_comentario=False)))
        monkeypatch.setattr("app.router.extract_shipping", AsyncMock(return_value=EnvioExtraido(
            nombre="Laura Gómez", direccion="Cra 43 # 10-20", ciudad="Medellín", telefono="3001112233",
            pregunta_o_comentario=False)))
        elegir(client, "p1_pedido")
        escribir(client, "2 de oro")
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Medellín, 3001112233")
        elegir(client, "p8_confirmar", "button_reply")
        asunto, ficha = mocks["notify"].call_args.args
        assert "Pedido armado" in asunto
        assert "• 2 × Sal de oro — $52.000" in ficha
        assert "Total: $61.000" in ficha
        assert "Envío a: Laura Gómez, Cra 43 # 10-20, Medellín · Tel. 3001112233" in ficha
        assert "Etiquetas: Producto, Pendiente de pago" in ficha
        assert "P6 → P7 → P8 → P11" in ficha


# ── Ecos de la app (coexistencia) ─────────────────────────────────────────────

class TestEcosDelEquipo:
    def test_respuesta_del_equipo_se_registra(self, client, mocks):
        escribir(client, "asesor")
        equipo_escribe(client, "¡Hola Laura! Soy Juanita, ya te ayudo")
        conv = load_conversation(REMITENTE)
        assert conv.en_traspaso and conv.traspaso["equipo_respondio"]

    def test_equipo_interviene_sin_traspaso_silencia_al_bot(self, client, mocks):
        equipo_escribe(client, "Hola Laura, te escribo por tu pedido de ayer")
        assert load_conversation(REMITENTE).traspaso["motivo"] == "equipo_intervino"
        escribir(client, "¡Gracias!")
        mocks["ask"].assert_not_called()
        mocks["notify"].assert_not_called()

    def test_frase_de_cierre_reactiva_el_bot(self, client, mocks):
        escribir(client, "asesor")
        equipo_escribe(client, "¡Fue un gusto ayudarte! Te dejo de nuevo con Chatsabor 🌿")
        assert mocks["text"].call_args.args[1].startswith("¡Aquí sigo!")
        conv = load_conversation(REMITENTE)
        assert not conv.en_traspaso and conv.flujo_activo is None
        escribir(client, "hola")
        mocks["list"].assert_called_once()  # el bot volvió a responder

    def test_eco_duplicado_se_ignora(self, client, mocks):
        escribir(client, "asesor")
        payload = _payload("smb_message_echoes", {"message_echoes": [{
            "from": NEGOCIO, "to": REMITENTE, "id": "wamid.eco-dup", "timestamp": "1",
            "type": "text", "text": {"body": "Te dejo de nuevo con nuestro asistente"}}]})
        client.post("/webhook", json=payload)
        client.post("/webhook", json=payload)
        assert mocks["text"].call_count == 2  # T1 y una sola vez T3

    def test_equipo_inactivo_el_bot_retoma(self, client, mocks):
        conv = Conversation(remitente=REMITENTE, en_traspaso=True, traspaso={
            "motivo": "solicitud", "prioridad": "normal", "inicio": time.time() - 20 * 3600,
            "equipo_respondio": True, "ultima_respuesta_equipo": time.time() - (settings.handoff_idle_hours + 1) * 3600,
            "alerta_enviada": False})
        save_conversation(conv)
        escribir(client, "hola")
        mocks["list"].assert_called_once()
        assert not load_conversation(REMITENTE).en_traspaso


# ── Alerta de «nadie respondió» ───────────────────────────────────────────────

class TestAlerta:
    def _en_traspaso(self, inicio: datetime, **extra) -> None:
        conv = Conversation(remitente=REMITENTE, nombre="Laura", en_traspaso=True, traspaso={
            "motivo": "solicitud", "prioridad": "normal", "inicio": inicio.timestamp(),
            "equipo_respondio": False, "ultima_respuesta_equipo": None, "alerta_enviada": False, **extra})
        save_conversation(conv)

    def test_alerta_tras_una_hora_habil_sin_respuesta(self, mocks):
        self._en_traspaso(datetime(2026, 10, 21, 10, 0, tzinfo=COLOMBIA))
        asyncio.run(router.check_unanswered_handoffs(datetime(2026, 10, 21, 11, 5, tzinfo=COLOMBIA)))
        asunto = mocks["notify"].call_args.args[0]
        assert "Sin respuesta hace 65 min" in asunto and "Laura" in asunto
        assert load_conversation(REMITENTE).traspaso["alerta_enviada"]
        asyncio.run(router.check_unanswered_handoffs(datetime(2026, 10, 21, 11, 30, tzinfo=COLOMBIA)))
        mocks["notify"].assert_called_once()  # no se repite

    def test_sin_alerta_antes_de_tiempo_ni_fuera_de_horario(self, mocks):
        self._en_traspaso(datetime(2026, 10, 24, 17, 30, tzinfo=COLOMBIA))  # sábado 5:30 p. m.
        asyncio.run(router.check_unanswered_handoffs(datetime(2026, 10, 25, 12, 0, tzinfo=COLOMBIA)))  # domingo
        mocks["notify"].assert_not_called()

    def test_sin_alerta_si_el_equipo_ya_respondio(self, mocks):
        self._en_traspaso(datetime(2026, 10, 21, 10, 0, tzinfo=COLOMBIA), equipo_respondio=True)
        asyncio.run(router.check_unanswered_handoffs(datetime(2026, 10, 21, 15, 0, tzinfo=COLOMBIA)))
        mocks["notify"].assert_not_called()


def test_ficha_sin_pedido_muestra_ultimos_mensajes():
    conv = Conversation(remitente=REMITENTE, nombre="Laura", ruta=["G1", "P1", "P2"],
                        traspaso={"motivo": "solicitud", "prioridad": "normal", "inicio": time.time()})
    conv.add_turn("¿la sal rosa sirve para hipertensión?", "Prefiero que una persona del equipo lo revise contigo")
    ficha = handoff_ficha(conv)
    assert "Ruta: G1 → P1 → P2" in ficha
    assert "Cliente: ¿la sal rosa sirve para hipertensión?" in ficha


# ── Correo al equipo ──────────────────────────────────────────────────────────

class TestNotificacion:
    def test_sin_configuracion_no_envia(self, monkeypatch):
        from app import notify

        monkeypatch.setattr(notify.settings, "handoff_email_to", "")
        assert asyncio.run(notify.notify_team("asunto", "ficha")) is False

    def test_envia_por_smtp(self, monkeypatch):
        from app import notify

        enviados = []

        class SMTPFalso:
            def __init__(self, host, port, timeout):
                self.host = host

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                pass

            def send_message(self, message):
                enviados.append(message)

        for campo, valor in [("handoff_email_to", "equipo@saboryarte.test"), ("smtp_host", "smtp.test"),
                             ("smtp_user", "bot@saboryarte.test"), ("smtp_password", "clave-prueba")]:
            monkeypatch.setattr(notify.settings, campo, valor)
        monkeypatch.setattr(notify.smtplib, "SMTP", SMTPFalso)
        assert asyncio.run(notify.notify_team("[Saboryarte] Traspaso", "Cliente: Laura")) is True
        assert enviados[0]["To"] == "equipo@saboryarte.test"
        assert enviados[0].get_content().strip() == "Cliente: Laura"
