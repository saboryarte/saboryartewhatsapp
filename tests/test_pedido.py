"""Toma de pedidos: P6 (qué y cuánto) → P7 (envío) → P8 (resumen con cuentas en código)."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.claude import EnvioExtraido, LineaPedido, PedidoExtraido
from app.main import app
from app.orders import order_summary, shipping_cost, subtotal
from app.state import load_conversation

REMITENTE = "573001112233"
ENVIO_COMPLETO = EnvioExtraido(
    nombre="Laura Gómez", direccion="Cra 43 # 10-20", ciudad="Envigado", telefono="3001112233", pregunta_o_comentario=False,
)


def pedido(*lineas, no_reconocidos=(), distribuir=False, pregunta=False) -> PedidoExtraido:
    return PedidoExtraido(
        lineas=[LineaPedido(producto=p, cantidad=c) for p, c in lineas],
        no_reconocidos=list(no_reconocidos), quiere_distribuir=distribuir, pregunta_o_comentario=pregunta,
    )


def envio(**campos) -> EnvioExtraido:
    base = {"nombre": None, "direccion": None, "ciudad": None, "telefono": None, "pregunta_o_comentario": False}
    return EnvioExtraido(**{**base, **campos})


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mocks(monkeypatch):
    m = {name: AsyncMock(return_value=True) for name in ("text", "list", "buttons")}
    m["ask"] = AsyncMock(return_value="respuesta de Claude")
    m["order"] = AsyncMock()
    m["shipping"] = AsyncMock()
    for name, attr in [("ask", "ask_claude"), ("text", "send_text"), ("list", "send_list"), ("buttons", "send_buttons"),
                       ("order", "extract_order"), ("shipping", "extract_shipping")]:
        monkeypatch.setattr(f"app.router.{attr}", m[name])
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


_contador = iter(range(10_000))


def elegir(client, opcion_id: str, tipo: str = "list_reply") -> None:
    client.post("/webhook", json=_payload({"id": f"wamid.o{next(_contador)}", "type": "interactive", "interactive": {
        "type": tipo, tipo: {"id": opcion_id, "title": opcion_id},
    }}))


def escribir(client, body: str) -> None:
    client.post("/webhook", json=_payload({"id": f"wamid.p{next(_contador)}", "type": "text", "text": {"body": body}}))


def ultimo_texto(mocks) -> str:
    return mocks["text"].call_args.args[1]


# ── Cálculos ──────────────────────────────────────────────────────────────────

class TestCalculos:
    @pytest.mark.parametrize("ciudad, tarifa", [
        ("Medellín", 9_000), ("medellin, Antioquia", 9_000), ("Envigado", 12_000), ("Itagüí", 12_000),
        ("La Estrella", 12_000), ("Bello", 12_000), ("Bogotá", 18_000), ("Cali", 18_000),
    ])
    def test_tarifa_de_envio(self, ciudad, tarifa):
        assert shipping_cost(ciudad) == tarifa

    def test_subtotal_mezclas_kit_mini_y_kit_armado(self):
        items = [
            {"producto": "sal_de_oro", "cantidad": 2},             # 52.000
            {"producto": "kit_mini", "cantidad": 1},               # 60.000
            {"kit": ["sal_de_sol", "sal_chilemon", "sal_de_sol"]}, # kit de 3: 70.000
        ]
        assert subtotal(items) == 182_000

    def test_resumen_con_totales(self):
        texto = order_summary(
            [{"producto": "sal_de_oro", "cantidad": 2}, {"producto": "sal_chilemon", "cantidad": 1}],
            {"nombre": "Laura", "direccion": "Cra 43", "ciudad": "Envigado", "telefono": "300"},
        )
        assert "• 2 × Sal de oro — $52.000" in texto
        assert "Subtotal: $78.000" in texto and "Envío: $12.000 (Envigado)" in texto and "*Total: $90.000*" in texto


# ── Flujo completo ────────────────────────────────────────────────────────────

class TestFlujoDePedido:
    def test_pedido_completo_desde_hacer_mi_pedido(self, client, mocks):
        elegir(client, "p1_pedido")
        assert "¿Qué mezclas quieres" in ultimo_texto(mocks)

        mocks["order"].return_value = pedido(("sal_de_oro", 2), ("sal_chilemon", 1))
        escribir(client, "2 de oro y una chilemon")
        assert "¿A qué ciudad" in ultimo_texto(mocks)
        assert load_conversation(REMITENTE).paso == "P7"

        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        _, resumen, botones = mocks["buttons"].call_args.args
        assert "*Total: $90.000*" in resumen
        assert [b["id"] for b in botones] == ["p8_confirmar", "p8_cambiar", "p8_persona"]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P8"
        assert conv.datos["pedido"] == [{"producto": "sal_de_oro", "cantidad": 2}, {"producto": "sal_chilemon", "cantidad": 1}]

    def test_con_producto_elegido_pregunta_cantidad_y_pasa_contexto(self, client, mocks):
        elegir(client, "p3_sol", "button_reply")
        assert "*Sal de sol*" in ultimo_texto(mocks)
        mocks["order"].return_value = pedido(("sal_de_sol", 3))
        escribir(client, "tres")
        assert mocks["order"].call_args.args == ("tres", "Sal de sol", None)

    def test_kit_armado_valida_cantidad_de_mezclas(self, client, mocks):
        elegir(client, "kit_3")
        assert "kit de 3 mezclas" in ultimo_texto(mocks)
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1))
        escribir(client, "oro y sol")
        assert "me nombraste 2" in ultimo_texto(mocks)
        assert load_conversation(REMITENTE).paso == "P6"

        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1), ("tierra_de_fuego", 1))
        escribir(client, "oro, sol y la ahumada")
        assert load_conversation(REMITENTE).datos["pedido"] == [{"kit": ["sal_de_oro", "sal_de_sol", "tierra_de_fuego"]}]

    def test_kit_no_acepta_kit_mini_como_mezcla(self, client, mocks):
        elegir(client, "kit_2")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("kit_mini", 1))
        escribir(client, "oro y el kit mini")
        assert "me nombraste 1" in ultimo_texto(mocks)

    def test_productos_no_reconocidos_vuelve_a_preguntar(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(no_reconocidos=["sal negra"])
        escribir(client, "quiero sal negra")
        texto = ultimo_texto(mocks)
        assert "No tenemos sal negra" in texto and "Sal de oro" in texto
        assert load_conversation(REMITENTE).paso == "P6"

    def test_quiere_distribuir(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(distribuir=True)
        escribir(client, "quiero granel para mi tienda")
        assert "distribuidores" in ultimo_texto(mocks)

    def test_pregunta_a_mitad_del_pedido_la_responde_claude(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(pregunta=True)
        escribir(client, "¿cuánto demora el envío?")
        mocks["ask"].assert_called_once()
        assert ultimo_texto(mocks) == "respuesta de Claude"
        assert load_conversation(REMITENTE).paso == "P6"

    def test_error_de_extraccion_pide_repetir(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = None
        escribir(client, "2 de oro")
        assert "problema técnico" in ultimo_texto(mocks)

    def test_envio_incompleto_pide_lo_que_falta_y_acumula(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")

        mocks["shipping"].return_value = envio(ciudad="Bogotá", nombre="Laura Gómez")
        escribir(client, "Laura Gómez, Bogotá")
        assert "la dirección y un teléfono de contacto" in ultimo_texto(mocks)

        mocks["shipping"].return_value = envio(direccion="Calle 100 # 15-20", telefono="3001112233")
        escribir(client, "Calle 100 # 15-20, 3001112233")
        resumen = mocks["buttons"].call_args.args[1]
        assert "Envío: $18.000 (Bogotá)" in resumen and "*Total: $44.000*" in resumen

    def test_pregunta_a_mitad_del_envio_la_responde_claude(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = envio(pregunta_o_comentario=True)
        escribir(client, "¿hacen envíos a Pasto?")
        mocks["ask"].assert_called_once()
        assert load_conversation(REMITENTE).paso == "P7"


class TestResumen:
    @pytest.fixture
    def en_resumen(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(("sal_de_oro", 2))
        escribir(client, "2 de oro")
        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        mocks["buttons"].reset_mock()
        mocks["text"].reset_mock()

    def test_confirmar_pasa_el_pedido_al_equipo(self, client, mocks, en_resumen):
        elegir(client, "p8_confirmar", "button_reply")
        assert ultimo_texto(mocks).startswith("¡Pedido recibido!")
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P11" and conv.en_traspaso
        assert conv.etiquetas == ["Producto", "Pendiente de pago"]

    def test_cambiar_algo_conserva_el_envio_y_vuelve_al_resumen(self, client, mocks, en_resumen):
        elegir(client, "p8_cambiar", "button_reply")
        assert "¿Qué mezclas quieres" in ultimo_texto(mocks)
        mocks["order"].return_value = pedido(("sal_de_la_vid", 1))
        escribir(client, "mejor una de la vid")
        resumen = mocks["buttons"].call_args.args[1]
        assert "1 × Sal de la vid" in resumen and "Sal de oro" not in resumen
        mocks["shipping"].assert_called_once()  # no se volvió a pedir el envío
