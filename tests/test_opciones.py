"""Reglas para que el cliente nunca quede sin opciones (escribir = tocar, retomar el paso, atajos, no entender)."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.claude import EnvioExtraido
from app.greetings import is_greeting
from app.main import app
from app.state import load_conversation

REMITENTE = "573001112233"


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
    m["shipping"] = AsyncMock(return_value=EnvioExtraido(
        nombre=None, direccion=None, ciudad=None, telefono=None, pregunta_o_comentario=True))
    monkeypatch.setattr("app.router.extract_shipping", m["shipping"])
    return m


def _payload(message: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "p1"},
        "contacts": [{"profile": {"name": "Laura"}, "wa_id": REMITENTE}],
        "messages": [{"from": REMITENTE, "timestamp": "1", **message}],
    }}]}]}


_contador = iter(range(10_000))


def escribir(client, body: str) -> None:
    client.post("/webhook", json=_payload({"id": f"wamid.o{next(_contador)}", "type": "text", "text": {"body": body}}))


def elegir(client, opcion_id: str, tipo: str = "list_reply") -> None:
    client.post("/webhook", json=_payload({"id": f"wamid.c{next(_contador)}", "type": "interactive", "interactive": {
        "type": tipo, tipo: {"id": opcion_id, "title": opcion_id}}}))


class TestEscribirEsTocar:
    def test_escribir_el_texto_de_una_opcion_la_elige(self, client, mocks):
        escribir(client, "hola")                # menú G1
        escribir(client, "Comprar mezclas")     # = tocar «🧂 Comprar mezclas»
        assert [f["id"] for f in mocks["list"].call_args.args[3]][0] == "p1_catalogo"
        assert load_conversation(REMITENTE).paso == "P1"
        mocks["ask"].assert_not_called()

    def test_parte_unica_del_nombre_tambien_cuenta(self, client, mocks):
        elegir(client, "p1_catalogo")           # lista de las 6 mezclas
        escribir(client, "chilemon")            # solo «Sal chilemon» la contiene
        assert "*Sal chilemon*" in mocks["buttons"].call_args.args[1]

    def test_texto_ambiguo_no_elige(self, client, mocks):
        elegir(client, "p1_catalogo")
        escribir(client, "sal de")              # varias mezclas lo contienen: va a Claude
        mocks["ask"].assert_called_once()

    def test_escribir_el_boton_de_la_lista_la_vuelve_a_mostrar(self, client, mocks):
        elegir(client, "p1_pedido")             # lista con botón «Ver productos»
        escribir(client, "Ver productos")
        assert mocks["list"].call_count == 2
        assert mocks["list"].call_args.args[2] == "Ver productos"
        mocks["ask"].assert_not_called()

    def test_cantidad_con_botones_escrita(self, client, mocks):
        elegir(client, "pedir_sal_de_oro")      # botones 1 / 2 / 3
        escribir(client, "2")
        assert "• 2 × Sal de oro — $52.000" in mocks["buttons"].call_args.args[1]


class TestPreguntaEnUnPaso:
    def test_la_respuesta_llega_con_las_opciones_del_paso(self, client, mocks):
        elegir(client, "p1_kits")               # botones Kit mini / Armar mi kit / Regalo empresarial
        escribir(client, "¿el kit mini sirve para regalo?")
        cuerpo, botones = mocks["buttons"].call_args.args[1], mocks["buttons"].call_args.args[2]
        assert cuerpo == "respuesta de Claude"
        assert [b["id"] for b in botones] == ["p4_kit_mini", "p4_armar", "p4_regalo"]

    def test_fuera_de_un_paso_llega_con_ver_menu_y_persona(self, client, mocks):
        elegir(client, "faq_uso")               # P9C (texto) + P9S («¿Te sirvió?»)
        elegir(client, "p9s_si", "button_reply")
        elegir(client, "p1_catalogo")
        elegir(client, "prod_sal_de_oro")
        elegir(client, "p2f_agregar", "button_reply")
        elegir(client, "cantidad_1", "button_reply")
        elegir(client, "carrito_listo", "button_reply")  # P7: pide datos de envío (texto)
        escribir(client, "¿hacen envíos a Pasto?")
        cuerpo, botones = mocks["buttons"].call_args.args[1], mocks["buttons"].call_args.args[2]
        assert cuerpo.startswith("respuesta de Claude") and "¿A qué ciudad" in cuerpo  # repite lo pendiente
        assert [b["id"] for b in botones] == ["atajo_menu", "atajo_persona"]
        assert load_conversation(REMITENTE).paso == "P7"  # sigue esperando los datos

    def test_ver_menu_abre_el_menu(self, client, mocks):
        elegir(client, "atajo_menu", "button_reply")
        assert mocks["list"].call_args.args[3][0]["id"] == "menu_comprar"


class TestNoEntiende:
    def test_primera_vez_retoma_con_las_opciones(self, client, mocks):
        elegir(client, "p1_kits")
        mocks["ask"].return_value = "NO_ENTIENDO"
        escribir(client, "asdf qwe")
        cuerpo = mocks["buttons"].call_args.args[1]
        assert cuerpo.startswith("Uy, creo que no te entendí bien 🙈")
        assert "NO_ENTIENDO" not in cuerpo
        assert not load_conversation(REMITENTE).en_traspaso

    def test_segunda_vez_seguida_pasa_a_una_persona(self, client, mocks):
        elegir(client, "p1_kits")
        mocks["ask"].return_value = "NO_ENTIENDO"
        escribir(client, "asdf qwe")
        escribir(client, "zxcv")
        assert mocks["text"].call_args.args[1].startswith("Mejor te paso con alguien del equipo")
        conv = load_conversation(REMITENTE)
        assert conv.en_traspaso and conv.traspaso["motivo"] == "no_entiende"
        assert "no entendió" in mocks["notify"].call_args.args[0]

    def test_entender_en_medio_reinicia_el_conteo(self, client, mocks):
        elegir(client, "p1_kits")
        mocks["ask"].return_value = "NO_ENTIENDO"
        escribir(client, "asdf")
        mocks["ask"].return_value = "respuesta de Claude"
        escribir(client, "¿cuánto cuesta el kit mini?")
        mocks["ask"].return_value = "NO_ENTIENDO"
        escribir(client, "zxcv")
        assert not load_conversation(REMITENTE).en_traspaso

    def test_primer_mensaje_incomprensible_muestra_el_menu(self, client, mocks):
        mocks["ask"].return_value = "NO_ENTIENDO"
        escribir(client, "jjjkk")
        cuerpo = mocks["list"].call_args.args[1]
        assert "NO_ENTIENDO" not in cuerpo
        assert mocks["list"].call_args.args[3][0]["id"] == "menu_comprar"


@pytest.mark.parametrize("texto", ["Buenaaaas!", "buenooos dias", "Holaaa"])
def test_saludos_con_letras_repetidas(texto):
    assert is_greeting(texto)


class TestLlevarALaOpcion:
    def test_quiero_un_kit_lleva_a_los_kits(self, client, mocks):
        elegir(client, "p1_catalogo")
        mocks["ask"].return_value = "IR:p1_kits"
        escribir(client, "Mejor quiero un kit")
        botones = mocks["buttons"].call_args.args[2]
        assert [b["id"] for b in botones] == ["p4_kit_mini", "p4_armar", "p4_regalo"]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P4" and conv.historial[-2]["content"] == "Elegí «Kit mini y kits»"

    def test_kit_de_3_abre_la_seleccion_de_mezclas(self, client, mocks):
        mocks["ask"].return_value = "IR:kit_3"
        escribir(client, "hola")                          # menú
        escribir(client, "quiero armar un kit de 3 mezclas")
        assert "kit de 3 mezclas" in mocks["list"].call_args.args[1]  # sin Flow en pruebas: lista de una en una
        assert load_conversation(REMITENTE).datos["kit"] == 3

    def test_primer_mensaje_con_intencion_va_directo(self, client, mocks):
        mocks["ask"].return_value = "IR:p1_pedido"
        escribir(client, "quiero hacer un pedido")
        assert mocks["list"].call_args.args[2] == "Ver productos"

    def test_destino_no_permitido_no_navega_ni_muestra_el_codigo(self, client, mocks):
        escribir(client, "hola")
        mocks["ask"].return_value = "IR:p8_confirmar"  # no está entre los destinos permitidos
        escribir(client, "confírmame el pedido")
        enviados = [c.args[1] for m in ("text", "list", "buttons") for c in mocks[m].call_args_list]
        assert not any("IR:" in texto for texto in enviados)
        assert mocks["list"].call_args.args[1].startswith("Te muestro las opciones para seguir")
        assert load_conversation(REMITENTE).paso != "P11"


def test_destinos_existen_en_los_mensajes():
    from app.flows import find_any_option
    from app.prompt import DESTINOS

    for option_id in DESTINOS:
        assert find_any_option(option_id), option_id
