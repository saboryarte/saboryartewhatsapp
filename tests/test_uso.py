"""Registro del costo real: tokens de Claude y mensajes enviados, por conversación."""
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import claude, usage, whatsapp
from app.main import app

REMITENTE = "573001112233"
# Respuesta típica medida en evals/: system prompt leído de la caché, algo de historial y ~150 tokens de salida.
USO_SONNET = SimpleNamespace(input_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=3613, output_tokens=156)


@pytest.fixture
def reloj(monkeypatch):
    """Hora controlada para los eventos registrados."""
    ahora = {"t": 1_790_000_000.0}
    monkeypatch.setattr(usage.time, "time", lambda: ahora["t"])
    return ahora


@pytest.fixture
def graph(monkeypatch):
    """Graph API falsa que acepta todo lo enviado."""
    enviados = []

    def handler(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    cliente_real = httpx.AsyncClient
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", lambda: cliente_real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(whatsapp.settings, "whatsapp_token", "token-prueba")
    monkeypatch.setattr(whatsapp.settings, "whatsapp_phone_number_id", "123")
    return enviados


@pytest.fixture
def sonnet_falso(monkeypatch):
    """Reemplaza la API de Anthropic en ask_claude por una respuesta fija con su uso."""

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_final_message(self):
            return SimpleNamespace(
                stop_reason="end_turn", usage=USO_SONNET, content=[SimpleNamespace(type="text", text="Claro 🌿")]
            )

    monkeypatch.setattr(claude, "_client", SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream())))


def test_costo_de_claude_con_cache_leida():
    # 500 × 2 + 3.613 × 0,20 + 156 × 10, por millón
    assert usage.claude_cost("claude-sonnet-5-5", USO_SONNET) == pytest.approx(0.0032826)


def test_la_escritura_de_cache_cuesta_1_25_veces_la_entrada():
    escritura = SimpleNamespace(input_tokens=0, cache_creation_input_tokens=1_000_000, cache_read_input_tokens=0, output_tokens=0)
    assert usage.claude_cost("claude-sonnet-5-5", escritura) == pytest.approx(2.5)


def test_mensaje_libre_registra_claude_y_mensajes_a_nombre_del_cliente(graph, sonnet_falso):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"profile": {"name": "Laura"}, "wa_id": REMITENTE}],
            "messages": [{"from": REMITENTE, "id": "m1", "timestamp": "1", "type": "text",
                          "text": {"body": "¿Cuánto cuesta el envío a Cali?"}}],
        }}]}],
    }
    assert TestClient(app).post("/webhook", json=payload).status_code == 200

    [conversacion] = usage.conversation_costs(0)
    assert conversacion["remitente"] == REMITENTE
    assert conversacion["llamadas_claude"] == 1
    assert conversacion["cache_lectura"] == 3613
    assert conversacion["costo_claude"] == pytest.approx(0.0032826)
    # Cada mensaje que Meta aceptó queda contado.
    assert conversacion["mensajes"] == len(graph) >= 1
    # Fuera de un mensaje del cliente, las llamadas ya no se atribuyen a nadie.
    assert usage.remitente_actual.get() is None


def test_una_pausa_de_mas_de_24_h_abre_otra_conversacion(reloj):
    usage.record_whatsapp(REMITENTE, "text")
    reloj["t"] += 3600
    usage.record_whatsapp(REMITENTE, "interactive")
    reloj["t"] += 25 * 3600
    usage.record_whatsapp(REMITENTE, "text")
    usage.record_whatsapp("573009998877", "text")

    conversaciones = usage.conversation_costs(0)
    assert [(c["remitente"], c["mensajes"]) for c in conversaciones] == [
        (REMITENTE, 2), (REMITENTE, 1), ("573009998877", 1),
    ]


def test_whatsapp_solo_cobra_lo_que_pasa_de_los_gratuitos_del_mes(reloj, monkeypatch):
    monkeypatch.setattr(usage, "SERVICIO_GRATIS_MES", 2)
    for _ in range(5):
        usage.record_whatsapp(REMITENTE, "text")

    [mes] = usage.whatsapp_costs_by_month(reloj["t"])
    assert (mes["enviados"], mes["cobrables"]) == (5, 3)
    assert mes["costo"] == pytest.approx(3 * usage.TARIFA_SERVICIO_USD)


def test_si_falla_el_registro_no_se_interrumpe_la_respuesta(monkeypatch):
    def sin_base():
        raise sqlite3.OperationalError("disco lleno")

    monkeypatch.setattr(usage, "_connect", sin_base)
    usage.record_whatsapp(REMITENTE, "text")
    usage.record_claude("claude-sonnet-5-5", USO_SONNET)


def test_reporte_muestra_totales_sin_el_numero_completo(reloj):
    token = usage.remitente_actual.set(REMITENTE)
    usage.record_claude("claude-sonnet-5-5", USO_SONNET)
    usage.remitente_actual.reset(token)
    usage.record_whatsapp(REMITENTE, "text")

    reporte = usage.format_report(30, now=reloj["t"] + 60)
    assert "…2233" in reporte and REMITENTE not in reporte
    assert "Conversaciones: 1" in reporte
    assert "Mensajes enviados: 1" in reporte
    assert "US$0,0033 por conversación" in reporte


def test_reporte_sin_uso():
    assert "Sin uso registrado" in usage.format_report(7)
