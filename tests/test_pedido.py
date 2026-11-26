"""Toma de pedidos: P6 o kits (qué y cuánto) → P7 (ciudad) → P7B/P7D (envío) → P8 (resumen) → P11 (pago)."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.claude import EnvioExtraido, LineaPedido, PedidoExtraido
from app.flows import get_step
from app.main import app
from app.orders import count_kits, describe_item, find_mixes, order_summary, parse_quantity, shipping_cost, subtotal
from app.state import load_conversation

REMITENTE = "573001112233"
ENVIO_COMPLETO = EnvioExtraido(
    nombre="Laura Gómez", direccion="Cra 43 # 10-20", ciudad="Envigado", telefono="3001112233", pregunta_o_comentario=False,
)


def pedido(*lineas, no_reconocidos=(), distribuir=False, pregunta=False, kits=None) -> PedidoExtraido:
    return PedidoExtraido(
        lineas=[LineaPedido(producto=p, cantidad=c) for p, c in lineas], kits=kits,
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
    m = {name: AsyncMock(return_value=True) for name in ("text", "list", "buttons", "notify")}
    m["ask"] = AsyncMock(return_value="respuesta de Claude")
    m["order"] = AsyncMock()
    m["shipping"] = AsyncMock()
    for name, attr in [("ask", "ask_claude"), ("text", "send_text"), ("list", "send_list"), ("buttons", "send_buttons"),
                       ("order", "extract_order"), ("shipping", "extract_shipping"), ("notify", "notify_team")]:
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
        assert "una persona" not in texto.lower()

    def test_resumen_al_resto_del_pais_avisa_que_el_envio_puede_variar(self):
        envio_ = {"nombre": "Laura", "direccion": "Cl 100", "ciudad": "Bogotá", "telefono": "300"}
        items = [{"producto": "sal_de_oro", "cantidad": 1}]
        assert "Envío: $18.000 (Bogotá) _Puede variar._" in order_summary(items, envio_, "Puede variar.")
        assert "_" not in order_summary(items, {**envio_, "ciudad": "Medellín"}, "Puede variar.")

    def test_varios_kits_iguales(self):
        item = {"kit": ["sal_de_oro", "sal_de_sol", "sal_chilemon"], "cantidad": 2}
        assert subtotal([item]) == 140_000
        assert describe_item(item) == "• 2 × Kit de 3 mezclas (Sal de oro, Sal de sol, Sal chilemon) — $140.000"
        assert describe_item({**item, "cantidad": 1}).startswith("• Kit de 3 mezclas")


# ── Flujo completo ────────────────────────────────────────────────────────────

class TestFlujoDePedido:
    def test_pedido_completo_desde_hacer_mi_pedido(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        assert "Armemos tu pedido" in ultimo_texto(mocks)

        mocks["order"].return_value = pedido(("sal_de_oro", 2), ("sal_chilemon", 1))
        escribir(client, "2 de oro y una chilemon")
        assert mocks["order"].call_args.args == ("2 de oro y una chilemon", None, None)
        assert "¿A qué ciudad" in ultimo_texto(mocks)
        assert load_conversation(REMITENTE).paso == "P7"

        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        _, resumen, botones = mocks["buttons"].call_args.args
        assert "*Total: $90.000*" in resumen
        assert [b["id"] for b in botones] == ["p8_confirmar", "p8_cambiar"]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P8"
        assert conv.datos["pedido"] == [{"producto": "sal_de_oro", "cantidad": 2}, {"producto": "sal_chilemon", "cantidad": 1}]

        elegir(client, "p8_confirmar", "button_reply")
        _, cuerpo, botones = mocks["buttons"].call_args.args
        assert cuerpo.startswith("¡Genial! Para dejar tu pedido en firme")
        assert [(b["id"], b["title"]) for b in botones] == [("p11_transferencia", "Transferencia"), ("p11_link", "Link de pago")]
        assert load_conversation(REMITENTE).paso == "P11"

    def test_solo_la_ciudad_da_el_valor_del_envio_y_pide_los_datos(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = envio(ciudad="Medellín")
        escribir(client, "Medellín")
        texto = ultimo_texto(mocks)
        assert texto.startswith("El envío a Medellín cuesta $9.000. Despachamos los martes y jueves")
        assert "1 a 2 días hábiles" in texto and "puede variar" not in texto
        assert texto.endswith("Para el envío necesito tu nombre completo, dirección y un teléfono de contacto.")
        assert load_conversation(REMITENTE).paso == "P7B"

        mocks["shipping"].return_value = envio(nombre="Laura Gómez", direccion="Cl 10 # 4-5", telefono="3001112233")
        escribir(client, "Laura Gómez, Cl 10 # 4-5, 3001112233")
        resumen = mocks["buttons"].call_args.args[1]
        assert "Envío: $9.000 (Medellín)" in resumen and "*Total: $35.000*" in resumen

    def test_resto_del_pais_avisa_que_el_envio_puede_variar(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = envio(ciudad="Bogotá")
        escribir(client, "Bogotá")
        texto = ultimo_texto(mocks)
        assert "$18.000" in texto and "2 a 4 días hábiles" in texto and "El valor puede variar" in texto

    def test_envio_incompleto_pide_lo_que_falta_y_acumula(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")

        mocks["shipping"].return_value = envio(ciudad="Bogotá", nombre="Laura Gómez")
        escribir(client, "Laura Gómez, Bogotá")
        assert "la dirección y un teléfono de contacto" in ultimo_texto(mocks)

        mocks["shipping"].return_value = envio(direccion="Calle 100 # 15-20", telefono="3001112233")
        escribir(client, "Calle 100 # 15-20, 3001112233")
        resumen = mocks["buttons"].call_args.args[1]
        assert "Envío: $18.000 (Bogotá)" in resumen and "*Total: $44.000*" in resumen

    def test_sin_ciudad_la_vuelve_a_pedir(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = envio(nombre="Laura Gómez")
        escribir(client, "Laura Gómez")
        assert ultimo_texto(mocks) == "¿A qué ciudad te lo enviamos? 🙌"
        assert load_conversation(REMITENTE).paso == "P7"

    def test_productos_no_reconocidos_vuelve_a_preguntar(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(no_reconocidos=["sal negra"])
        escribir(client, "quiero sal negra")
        texto = ultimo_texto(mocks)
        assert "No tenemos sal negra" in texto and "Sal de oro" in texto
        assert load_conversation(REMITENTE).paso == "P6"

    def test_quiere_distribuir_pasa_directo_a_juanita(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(distribuir=True)
        escribir(client, "quiero granel para mi tienda")
        assert "Te paso con Juanita, de nuestro equipo comercial" in ultimo_texto(mocks)
        conv = load_conversation(REMITENTE)
        assert conv.en_traspaso and conv.traspaso["motivo"] == "distribuidor" and "Distribuidor" in conv.etiquetas
        assert "Quiere distribuir" in mocks["notify"].call_args.args[0]

    def test_pregunta_a_mitad_del_pedido_la_responde_claude(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(pregunta=True)
        escribir(client, "¿cuánto demora el envío?")
        mocks["ask"].assert_called_once()
        cuerpo, botones = mocks["buttons"].call_args.args[1:3]
        assert cuerpo.startswith("respuesta de Claude") and "¿Cuáles quieres" in cuerpo  # repite lo pendiente
        assert [b["id"] for b in botones] == ["atajo_menu", "atajo_persona"]
        assert load_conversation(REMITENTE).paso == "P6"

    def test_error_de_extraccion_pide_repetir(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = None
        escribir(client, "2 de oro")
        assert "problema técnico" in ultimo_texto(mocks)

    def test_pregunta_a_mitad_del_envio_la_responde_claude(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = envio(pregunta_o_comentario=True)
        escribir(client, "¿hacen envíos a Pasto?")
        mocks["ask"].assert_called_once()
        assert load_conversation(REMITENTE).paso == "P7"


class TestKits:
    def test_kit_mini_con_la_cantidad_escrita(self, client, mocks):
        elegir(client, "p4_kit_mini", "button_reply")
        assert ultimo_texto(mocks) == "¡Buena elección! 🎁 ¿Cuántos kits mini quieres?"
        escribir(client, "tres")
        mocks["order"].assert_not_called()
        conv = load_conversation(REMITENTE)
        assert conv.datos["pedido"] == [{"producto": "kit_mini", "cantidad": 3}] and conv.paso == "P7"

    def test_kit_mini_con_una_frase(self, client, mocks):
        elegir(client, "p4_kit_mini", "button_reply")
        mocks["order"].return_value = pedido(("kit_mini", 2))
        escribir(client, "quiero 2 para regalar")
        assert mocks["order"].call_args.args == ("quiero 2 para regalar", "Kit mini")
        assert load_conversation(REMITENTE).datos["pedido"] == [{"producto": "kit_mini", "cantidad": 2}]

    def test_kit_mini_sin_cantidad_la_pregunta(self, client, mocks):
        elegir(client, "p4_kit_mini", "button_reply")
        mocks["order"].return_value = pedido()
        escribir(client, "mmm no sé")
        assert ultimo_texto(mocks) == "¿Cuántos kits mini quieres? Escríbeme solo el número 🙌"
        assert load_conversation(REMITENTE).paso == "P4M"

    def test_kit_armado_valida_cuantas_mezclas_y_cuenta_los_kits(self, client, mocks):
        elegir(client, "kit_3")
        assert ultimo_texto(mocks).startswith("¡Tu kit de 3 mezclas! 🎁 ¿Cuáles 3 quieres y cuántos kits?")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1))
        escribir(client, "oro y sol")
        assert mocks["order"].call_args.args == ("oro y sol", None, 3)
        assert "me nombraste 2" in ultimo_texto(mocks)
        assert load_conversation(REMITENTE).paso == "P4K"

        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1), ("tierra_de_fuego", 1), kits=2)
        escribir(client, "2 kits de oro, sol y la ahumada")
        conv = load_conversation(REMITENTE)
        assert conv.datos["pedido"] == [{"kit": ["sal_de_oro", "sal_de_sol", "tierra_de_fuego"], "cantidad": 2}]
        assert conv.paso == "P7" and "kit" not in conv.datos

    def test_kit_con_nombres_cortos_se_arma_sin_claude(self, client, mocks):
        elegir(client, "kit_4")
        assert "oro, sol, luna rosa, vid, chilemon o tierra de fuego" in ultimo_texto(mocks)
        escribir(client, "Sal de oro, sal Luna Rosa, sal de la vid y tierra de fuego.")
        mocks["order"].assert_not_called()
        conv = load_conversation(REMITENTE)
        assert conv.datos["pedido"] == [
            {"kit": ["sal_de_oro", "sal_de_luna_rosa", "sal_de_la_vid", "tierra_de_fuego"], "cantidad": 1}]
        assert conv.paso == "P7"

    def test_kit_con_nombres_cortos_y_cantidad_de_kits(self, client, mocks):
        elegir(client, "kit_3")
        escribir(client, "dos kits: chile limón, rosa y fuego")
        mocks["order"].assert_not_called()
        assert load_conversation(REMITENTE).datos["pedido"] == [
            {"kit": ["sal_chilemon", "sal_de_luna_rosa", "tierra_de_fuego"], "cantidad": 2}]

    def test_si_claude_falla_dice_cuantas_mezclas_reconocio(self, client, mocks):
        elegir(client, "kit_4")
        mocks["order"].return_value = None
        escribir(client, "oro y sol")
        assert ultimo_texto(mocks).startswith("Tu kit es de 4 mezclas y me nombraste 2")
        assert load_conversation(REMITENTE).paso == "P4K"

    def test_kit_no_acepta_kit_mini_como_mezcla(self, client, mocks):
        elegir(client, "kit_2")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("kit_mini", 1))
        escribir(client, "oro y el kit mini")
        assert "me nombraste 1" in ultimo_texto(mocks)

    def test_una_mezcla_repetida_cuenta_una_vez(self, client, mocks):
        elegir(client, "kit_2")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_oro", 1), ("sal_de_sol", 1))
        escribir(client, "oro, oro y sol")
        assert load_conversation(REMITENTE).datos["pedido"] == [{"kit": ["sal_de_oro", "sal_de_sol"], "cantidad": 1}]

    def test_kit_de_6_solo_pregunta_cuantos(self, client, mocks):
        elegir(client, "kit_6")
        assert ultimo_texto(mocks) == get_step("P4K")["texto_todas"]
        escribir(client, "2")
        mocks["order"].assert_not_called()
        item = load_conversation(REMITENTE).datos["pedido"][0]
        assert len(item["kit"]) == 6 and item["cantidad"] == 2

    def test_kit_con_envio_conocido_va_al_resumen(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        elegir(client, "kit_2")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1))
        escribir(client, "oro y sol")
        resumen = mocks["buttons"].call_args.args[1]
        assert "• Kit de 2 mezclas (Sal de oro, Sal de sol) — $48.000" in resumen


class TestResumen:
    @pytest.fixture
    def en_resumen(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_oro", 2))
        escribir(client, "2 de oro")
        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        mocks["buttons"].reset_mock()
        mocks["text"].reset_mock()

    def test_confirmar_pregunta_como_pagar_sin_traspaso(self, client, mocks, en_resumen):
        elegir(client, "p8_confirmar", "button_reply")
        assert mocks["buttons"].call_args.args[1] == get_step("P11")["texto"]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P11" and not conv.en_traspaso
        mocks["notify"].assert_not_called()

    def test_cambiar_algo_conserva_el_envio_y_vuelve_al_resumen(self, client, mocks, en_resumen):
        elegir(client, "p8_cambiar", "button_reply")
        assert "Armemos tu pedido" in ultimo_texto(mocks)
        mocks["order"].return_value = pedido(("sal_de_la_vid", 1))
        escribir(client, "mejor una de la vid")
        resumen = mocks["buttons"].call_args.args[1]
        assert "1 × Sal de la vid" in resumen and "Sal de oro" not in resumen
        mocks["shipping"].assert_called_once()  # no se volvió a pedir el envío

    def test_confirmar_un_resumen_viejo_vuelve_a_armar_el_pedido(self, client, mocks):
        elegir(client, "p8_confirmar", "button_reply")
        assert "Armemos tu pedido" in ultimo_texto(mocks)
        assert load_conversation(REMITENTE).paso == "P6"


class TestCantidadEscrita:
    @pytest.mark.parametrize("texto, cantidad", [
        ("4", 4), ("cuatro", 4), ("5 unidades", 5), ("una", 1), ("Dos.", 2), ("12", 12),
    ])
    def test_reconoce(self, texto, cantidad):
        assert parse_quantity(texto) == cantidad

    @pytest.mark.parametrize("texto", ["dos de oro", "¿cuánto cuesta?", "0", "500", "hola"])
    def test_no_es_una_cantidad(self, texto):
        assert parse_quantity(texto) is None


class TestNombresDeMezclas:
    @pytest.mark.parametrize("texto, esperado", [
        ("Sal de oro, sal Luna Rosa, sal de la vid y tierra de fuego.",
         ["sal_de_oro", "sal_de_luna_rosa", "sal_de_la_vid", "tierra_de_fuego"]),
        ("ORO, SOL y Chilemon", ["sal_de_oro", "sal_de_sol", "sal_chilemon"]),
        ("la de luna y la de chile limón", ["sal_de_luna_rosa", "sal_chilemon"]),
        ("vid, vid y fuego", ["sal_de_la_vid", "tierra_de_fuego"]),
        ("solo quiero ver precios", []),
    ])
    def test_reconoce_nombres_completos_y_cortos(self, texto, esperado):
        assert find_mixes(texto) == esperado

    @pytest.mark.parametrize("texto, kits", [("2 kits", 2), ("tres kits de oro", 3), ("un kit", 1), ("oro y sol", None)])
    def test_cuenta_los_kits(self, texto, kits):
        assert count_kits(texto) == kits
