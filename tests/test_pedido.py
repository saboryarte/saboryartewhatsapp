"""Toma de pedidos: P6 (qué y cuánto) → P7 (envío) → P8 (resumen con cuentas en código)."""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.claude import EnvioExtraido, LineaPedido, PedidoExtraido
from app.main import app
from app.orders import order_summary, parse_quantity, shipping_cost, subtotal
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
        assert "Armemos tu pedido" in mocks["list"].call_args.args[1]

        mocks["order"].return_value = pedido(("sal_de_oro", 2), ("sal_chilemon", 1))
        escribir(client, "2 de oro y una chilemon")
        assert "¿A qué ciudad" in ultimo_texto(mocks)
        assert load_conversation(REMITENTE).paso == "P7"

        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        _, resumen, botones = mocks["buttons"].call_args.args
        assert "*Total: $90.000*" in resumen
        assert [b["id"] for b in botones] == ["p8_confirmar", "p8_transferencia", "p8_cambiar"]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P8"
        assert conv.datos["pedido"] == [{"producto": "sal_de_oro", "cantidad": 2}, {"producto": "sal_chilemon", "cantidad": 1}]

    def test_con_producto_elegido_la_cantidad_escrita_no_usa_claude(self, client, mocks):
        elegir(client, "p3_sol", "button_reply")
        assert "*Sal de sol*" in mocks["buttons"].call_args.args[1]
        escribir(client, "tres")
        mocks["order"].assert_not_called()
        assert "• 3 × Sal de sol — $78.000" in mocks["buttons"].call_args.args[1]

    def test_texto_en_la_cantidad_pasa_el_producto_como_contexto(self, client, mocks):
        elegir(client, "p3_sol", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_sol", 2), ("sal_chilemon", 1))
        escribir(client, "dos de esas y una chilemon")
        assert mocks["order"].call_args.args == ("dos de esas y una chilemon", "Sal de sol", None)

    def test_kit_armado_por_texto_valida_cantidad_de_mezclas(self, client, mocks):
        elegir(client, "kit_3")
        assert "kit de 3 mezclas" in mocks["list"].call_args.args[1]
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1))
        escribir(client, "oro y sol")
        assert "me nombraste 2" in mocks["list"].call_args.args[1]
        assert load_conversation(REMITENTE).paso == "P4C"

        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_sol", 1), ("tierra_de_fuego", 1))
        escribir(client, "oro, sol y la ahumada")
        assert load_conversation(REMITENTE).datos["pedido"] == [{"kit": ["sal_de_oro", "sal_de_sol", "tierra_de_fuego"]}]

    def test_kit_no_acepta_kit_mini_como_mezcla(self, client, mocks):
        elegir(client, "kit_2")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("kit_mini", 1))
        escribir(client, "oro y el kit mini")
        assert "me nombraste 1" in mocks["list"].call_args.args[1]

    def test_productos_no_reconocidos_vuelve_a_preguntar(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(no_reconocidos=["sal negra"])
        escribir(client, "quiero sal negra")
        texto = mocks["list"].call_args.args[1]
        assert "No tenemos sal negra" in texto and "Sal de oro" in texto
        assert len(mocks["list"].call_args.args[3]) == 7  # vuelve la lista de productos
        assert load_conversation(REMITENTE).paso == "P6"

    def test_quiere_distribuir(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(distribuir=True)
        escribir(client, "quiero granel para mi tienda")
        assert "distribuidores" in mocks["list"].call_args.args[1]

    def test_pregunta_a_mitad_del_pedido_la_responde_claude(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(pregunta=True)
        escribir(client, "¿cuánto demora el envío?")
        mocks["ask"].assert_called_once()
        assert mocks["list"].call_args.args[1] == "respuesta de Claude"  # con la lista de productos
        assert load_conversation(REMITENTE).paso == "P6"

    def test_error_de_extraccion_pide_repetir(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = None
        escribir(client, "2 de oro")
        assert "problema técnico" in mocks["list"].call_args.args[1]

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


class TestPedidoConLista:
    def test_lista_de_productos_con_precio(self, client, mocks):
        elegir(client, "p1_pedido")
        _, cuerpo, boton, filas = mocks["list"].call_args.args
        assert boton == "Ver productos" and len(filas) == 7
        assert filas[0]["title"] == "Sal de oro" and filas[0]["description"].startswith("$26.000 · ")
        assert filas[-1]["title"] == "Kit mini" and filas[-1]["description"].startswith("$60.000 · ")
        assert all(len(f["description"]) <= 72 for f in filas)

    def test_pedido_completo_sin_escribir(self, client, mocks):
        elegir(client, "p1_pedido")
        elegir(client, "pedir_sal_de_oro")
        elegir(client, "cantidad_2", "button_reply")
        _, cuerpo, botones = mocks["buttons"].call_args.args
        assert "• 2 × Sal de oro — $52.000" in cuerpo and "Subtotal: $52.000" in cuerpo
        assert [b["id"] for b in botones] == ["carrito_agregar", "carrito_listo"]

        elegir(client, "carrito_agregar", "button_reply")
        assert mocks["list"].call_count == 2  # vuelve la lista de productos
        elegir(client, "pedir_kit_mini")
        elegir(client, "cantidad_1", "button_reply")
        cuerpo = mocks["buttons"].call_args.args[1]
        assert "• 1 × Kit mini — $60.000" in cuerpo and "Subtotal: $112.000" in cuerpo

        elegir(client, "carrito_listo", "button_reply")
        assert "¿A qué ciudad" in ultimo_texto(mocks)
        conv = load_conversation(REMITENTE)
        assert conv.datos["pedido"] == [{"producto": "sal_de_oro", "cantidad": 2}, {"producto": "kit_mini", "cantidad": 1}]
        assert "carrito" not in conv.datos
        mocks["order"].assert_not_called()

    def test_mismo_producto_dos_veces_suma_cantidades(self, client, mocks):
        elegir(client, "p1_pedido")
        elegir(client, "pedir_sal_chilemon")
        elegir(client, "cantidad_1", "button_reply")
        elegir(client, "carrito_agregar", "button_reply")
        elegir(client, "pedir_sal_chilemon")
        escribir(client, "4")
        assert "• 5 × Sal chilemon — $130.000" in mocks["buttons"].call_args.args[1]

    def test_lo_escrito_se_suma_a_lo_elegido(self, client, mocks):
        elegir(client, "p1_pedido")
        elegir(client, "pedir_sal_de_oro")
        elegir(client, "cantidad_1", "button_reply")
        mocks["order"].return_value = pedido(("sal_de_la_vid", 2))
        escribir(client, "agrégame también 2 de la vid")
        assert load_conversation(REMITENTE).datos["pedido"] == [
            {"producto": "sal_de_oro", "cantidad": 1}, {"producto": "sal_de_la_vid", "cantidad": 2}]

    def test_boton_de_cantidad_viejo_vuelve_a_la_lista(self, client, mocks):
        elegir(client, "cantidad_2", "button_reply")
        assert mocks["list"].call_args.args[2] == "Ver productos"


class TestCantidadEscrita:
    @pytest.mark.parametrize("texto, cantidad", [
        ("4", 4), ("cuatro", 4), ("5 unidades", 5), ("una", 1), ("Dos.", 2), ("12", 12),
    ])
    def test_reconoce(self, texto, cantidad):
        assert parse_quantity(texto) == cantidad

    @pytest.mark.parametrize("texto", ["dos de oro", "¿cuánto cuesta?", "0", "500", "hola"])
    def test_no_es_una_cantidad(self, texto):
        assert parse_quantity(texto) is None


class TestKitConFlow:
    @pytest.fixture
    def flow(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "whatsapp_kit_flow_id", "FLOW123")
        monkeypatch.setattr(settings, "whatsapp_flows_mode", "draft")
        envio_flow = AsyncMock(return_value=True)
        monkeypatch.setattr("app.router.send_flow", envio_flow)
        return envio_flow

    def respuesta_flow(self, client, mezclas: list[str], token: str = "kit-3") -> None:
        client.post("/webhook", json=_payload({"id": f"wamid.f{next(_contador)}", "type": "interactive", "interactive": {
            "type": "nfm_reply",
            "nfm_reply": {"name": "flow", "body": "Sent", "response_json": json.dumps({"flow_token": token, "mezclas": mezclas})},
        }}))

    def test_kit_envia_formulario_con_casillas(self, client, mocks, flow):
        elegir(client, "kit_3")
        destinatario, cuerpo, boton, flow_id, token, pantalla, datos, modo = flow.call_args.args
        assert destinatario == REMITENTE and "kit de 3 mezclas" in cuerpo and boton == "Elegir mezclas"
        assert (flow_id, token, pantalla, modo) == ("FLOW123", "kit-3", "KIT", "draft")
        assert datos["cantidad"] == 3 and datos["titulo"] == "Elige 3 mezclas"
        assert [m["id"] for m in datos["mezclas"]] == [
            "sal_de_oro", "sal_de_sol", "sal_de_luna_rosa", "sal_de_la_vid", "sal_chilemon", "tierra_de_fuego"]
        mocks["list"].assert_not_called()
        assert load_conversation(REMITENTE).paso == "P4F"

    def test_respuesta_del_formulario_arma_el_kit(self, client, mocks, flow):
        elegir(client, "kit_3")
        self.respuesta_flow(client, ["sal_de_oro", "sal_de_sol", "sal_chilemon"])
        conv = load_conversation(REMITENTE)
        assert conv.datos["pedido"] == [{"kit": ["sal_de_oro", "sal_de_sol", "sal_chilemon"]}]
        assert "¿A qué ciudad" in ultimo_texto(mocks)
        assert conv.historial[-2]["content"] == "Elegí para mi kit: Sal de oro, Sal de sol, Sal chilemon"

    def test_respuesta_con_cantidad_incorrecta_vuelve_a_los_tamaños(self, client, mocks, flow):
        elegir(client, "kit_3")
        self.respuesta_flow(client, ["sal_de_oro", "sal_de_sol"])
        assert [f["id"] for f in mocks["list"].call_args.args[3]][0] == "kit_2"
        assert "pedido" not in load_conversation(REMITENTE).datos

    def test_si_el_formulario_falla_usa_la_lista(self, client, mocks, flow):
        flow.return_value = False
        elegir(client, "kit_4")
        assert "Elige la mezcla 1 de 4" in mocks["list"].call_args.args[1]

    def test_sin_flow_configurado_usa_la_lista(self, client, mocks):
        elegir(client, "kit_4")
        assert "Elige la mezcla 1 de 4" in mocks["list"].call_args.args[1]

    def test_tambien_acepta_escribir_las_mezclas(self, client, mocks, flow):
        elegir(client, "kit_2")
        mocks["order"].return_value = pedido(("sal_de_oro", 1), ("sal_de_la_vid", 1))
        escribir(client, "oro y la vid")
        assert load_conversation(REMITENTE).datos["pedido"] == [{"kit": ["sal_de_oro", "sal_de_la_vid"]}]


class TestKitConLista:
    def ids_filas(self, mocks) -> list[str]:
        return [f["id"] for f in mocks["list"].call_args.args[3]]

    def test_elige_las_mezclas_una_por_una(self, client, mocks):
        elegir(client, "kit_3")
        cuerpo = mocks["list"].call_args.args[1]
        assert "kit de 3 mezclas" in cuerpo and "Elige la mezcla 1 de 3" in cuerpo
        filas = mocks["list"].call_args.args[3]
        assert len(filas) == 6 and all(f["description"] for f in filas)  # nombre + ingredientes y usos

        elegir(client, "kitmix_sal_de_oro")
        assert "Llevas: Sal de oro." in mocks["list"].call_args.args[1]
        assert "Elige la mezcla 2 de 3" in mocks["list"].call_args.args[1]
        assert "kitmix_sal_de_oro" not in self.ids_filas(mocks) and len(self.ids_filas(mocks)) == 5

        elegir(client, "kitmix_sal_chilemon")
        elegir(client, "kitmix_tierra_de_fuego")
        assert "¿A qué ciudad" in ultimo_texto(mocks)
        conv = load_conversation(REMITENTE)
        assert conv.datos["pedido"] == [{"kit": ["sal_de_oro", "sal_chilemon", "tierra_de_fuego"]}]
        assert conv.paso == "P7" and "kit_mezclas" not in conv.datos
        mocks["order"].assert_not_called()  # sin Claude: todo con la lista

    def test_tocar_dos_veces_la_misma_no_cuenta_doble(self, client, mocks):
        elegir(client, "kit_2")
        elegir(client, "kitmix_sal_de_sol")
        elegir(client, "kitmix_sal_de_sol")
        assert load_conversation(REMITENTE).datos["kit_mezclas"] == ["sal_de_sol"]

    def test_kit_de_6_no_pide_elegir_ni_agrega_pasos(self, client, mocks):
        elegir(client, "kit_6")
        mocks["list"].assert_not_called()
        mocks["text"].assert_called_once()  # un solo mensaje: aviso del kit + pregunta de envío
        texto = ultimo_texto(mocks)
        assert texto.startswith("¡Tu kit de 6 trae todas nuestras mezclas! ✨") and "¿A qué ciudad" in texto
        conv = load_conversation(REMITENTE)
        assert len(conv.datos["pedido"][0]["kit"]) == 6 and conv.paso == "P7"

    def test_lista_vieja_sin_kit_en_curso_vuelve_a_los_tamaños(self, client, mocks):
        elegir(client, "kitmix_sal_de_oro")
        assert self.ids_filas(mocks) == ["kit_2", "kit_3", "kit_4", "kit_5", "kit_6"]

    def test_kit_con_envio_conocido_va_al_resumen(self, client, mocks):
        elegir(client, "p1_pedido")
        mocks["order"].return_value = pedido(("sal_de_oro", 1))
        escribir(client, "una de oro")
        mocks["shipping"].return_value = ENVIO_COMPLETO
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        elegir(client, "kit_2")
        elegir(client, "kitmix_sal_de_oro")
        elegir(client, "kitmix_sal_de_sol")
        resumen = mocks["buttons"].call_args.args[1]
        assert "Kit de 2 mezclas (Sal de oro, Sal de sol) — $48.000" in resumen


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
        assert "Armemos tu pedido" in mocks["list"].call_args.args[1]
        mocks["order"].return_value = pedido(("sal_de_la_vid", 1))
        escribir(client, "mejor una de la vid")
        resumen = mocks["buttons"].call_args.args[1]
        assert "1 × Sal de la vid" in resumen and "Sal de oro" not in resumen
        mocks["shipping"].assert_called_once()  # no se volvió a pedir el envío
