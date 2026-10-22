"""Ruta de compra: navegación del catálogo (P1–P5, P9) y prompt de Claude con la base de conocimiento."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.flows import format_price, get_step, list_rows
from app.main import app
from app.prompt import system_prompt
from app.state import Conversation, load_conversation, save_conversation

ROOT = Path(__file__).resolve().parent.parent
REMITENTE = "573001112233"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mocks(monkeypatch):
    m = {name: AsyncMock(return_value=True) for name in ("text", "list", "buttons")}
    m["ask"] = AsyncMock(return_value="respuesta de Claude")
    monkeypatch.setattr("app.router.ask_claude", m["ask"])
    monkeypatch.setattr("app.router.send_text", m["text"])
    monkeypatch.setattr("app.router.send_list", m["list"])
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


_contador = iter(range(10_000))


def elegir(client, opcion_id: str, tipo: str = "list_reply") -> None:
    client.post("/webhook", json=_payload({"id": f"wamid.c{next(_contador)}", "type": "interactive", "interactive": {
        "type": tipo, tipo: {"id": opcion_id, "title": opcion_id},
    }}))


def escribir(client, body: str) -> None:
    client.post("/webhook", json=_payload({"id": f"wamid.t{next(_contador)}", "type": "text", "text": {"body": body}}))


def ids(filas: list[dict]) -> list[str]:
    return [f["id"] for f in filas]


class TestNavegacionCatalogo:
    def test_comprar_muestra_p1(self, client, mocks):
        elegir(client, "menu_comprar")
        _, cuerpo, boton, filas = mocks["list"].call_args.args
        assert cuerpo == get_step("P1")["texto"] and boton == "Ver opciones"
        assert ids(filas) == ids(list_rows("P1"))
        conv = load_conversation(REMITENTE)
        assert (conv.flujo_activo, conv.paso) == ("compra", "P1")

    def test_ver_mezclas_lista_los_6_productos(self, client, mocks):
        elegir(client, "p1_catalogo")
        filas = mocks["list"].call_args.args[3]
        assert [f["title"] for f in filas] == [
            "Sal de oro", "Sal de sol", "Sal de luna rosa", "Sal de la vid", "Sal chilemon", "Mezcla tierra de fuego",
        ]

    def test_elegir_mezcla_envia_ficha_con_botones(self, client, mocks):
        elegir(client, "prod_sal_de_la_vid")
        _, ficha, botones = mocks["buttons"].call_args.args
        assert "*Sal de la vid*" in ficha and "vino tinto" in ficha and "$26.000" in ficha
        assert ids(botones) == ["p2f_agregar", "p2f_otra", "p2f_menu"]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P2F" and conv.datos["producto"] == "sal_de_la_vid"

    def test_tierra_de_fuego_muestra_60_g(self, client, mocks):
        elegir(client, "prod_tierra_de_fuego")
        assert "60 g" in mocks["buttons"].call_args.args[1]

    def test_agregar_al_pedido_inicia_el_pedido(self, client, mocks):
        elegir(client, "prod_sal_de_oro")
        elegir(client, "p2f_agregar", "button_reply")
        assert "*Sal de oro*" in mocks["text"].call_args.args[1]
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P6" and conv.datos["producto"] == "sal_de_oro"

    def test_volver_al_menu_reinicia(self, client, mocks):
        elegir(client, "prod_sal_de_oro")
        elegir(client, "p2f_menu", "button_reply")
        assert ids(mocks["list"].call_args.args[3])[0] == "menu_comprar"
        conv = load_conversation(REMITENTE)
        assert conv.flujo_activo is None and conv.datos == {}

    def test_las_mas_queridas_envia_botones(self, client, mocks):
        elegir(client, "p1_queridas")
        _, cuerpo, botones = mocks["buttons"].call_args.args
        assert "Chilemon" in cuerpo
        assert ids(botones) == ["p3_oro", "p3_sol", "p3_chilemon"]

    def test_kits_y_armar_kit(self, client, mocks):
        elegir(client, "p1_kits")
        assert "$60.000" in mocks["buttons"].call_args.args[1]
        elegir(client, "p4_armar", "button_reply")
        assert ids(mocks["list"].call_args.args[3]) == ["kit_2", "kit_3", "kit_4", "kit_5", "kit_6"]

    def test_no_se_cual_recomienda_y_muestra_mezclas(self, client, mocks):
        elegir(client, "uso_carnes")
        recomendacion = mocks["text"].call_args.args[1]
        assert "Sal de la vid" in recomendacion and "Mezcla tierra de fuego" in recomendacion
        assert len(mocks["list"].call_args.args[3]) == 6  # lista P2 para elegir

    def test_persona_aun_en_construccion(self, client, mocks):
        elegir(client, "menu_persona")
        assert "persona" in mocks["text"].call_args.args[1]


class TestDudas:
    def test_tema_de_base_conocimiento_responde_claude_y_pregunta_si_sirvio(self, client, mocks):
        elegir(client, "faq_envios")
        prompt = mocks["ask"].call_args.args[0]
        assert "Envíos y tiempos" in prompt
        mocks["text"].assert_called_once_with(REMITENTE, "respuesta de Claude")
        assert ids(mocks["buttons"].call_args.args[2]) == ["p9s_si", "p9s_no"]

    def test_como_usar_envia_p9c_y_pregunta_si_sirvio(self, client, mocks):
        elegir(client, "faq_uso")
        assert mocks["text"].call_args.args[1] == get_step("P9C")["texto"]
        mocks["buttons"].assert_called_once()
        assert load_conversation(REMITENTE).paso == "P9S"

    def test_otra_duda_libre_responde_claude_y_pregunta_si_sirvio(self, client, mocks):
        elegir(client, "faq_otra")
        assert mocks["text"].call_args.args[1] == get_step("P9O")["texto"]
        escribir(client, "¿Las mezclas tienen conservantes?")
        mocks["ask"].assert_called_once()
        assert ids(mocks["buttons"].call_args.args[2]) == ["p9s_si", "p9s_no"]
        historial = load_conversation(REMITENTE).historial
        assert historial[-1]["content"].startswith("respuesta de Claude")

    def test_si_me_sirvio_vuelve_a_p1(self, client, mocks):
        elegir(client, "p9s_si", "button_reply")
        assert ids(mocks["list"].call_args.args[3]) == ids(list_rows("P1"))


class TestPromptDeClaude:
    def test_incluye_la_base_publica(self):
        prompt = system_prompt()
        assert "<base_de_conocimiento>" in prompt
        assert "Sal de luna rosa" in prompt and "$26.000" in prompt and "$18.000" in prompt

    def test_no_incluye_informacion_interna(self):
        prompt = system_prompt()
        assert "18.500" not in prompt and "160 por gramo" not in prompt

    def test_catalogo_coincide_con_la_base_de_conocimiento(self):
        catalogo = json.loads((ROOT / "app/flows/catalogo.json").read_text(encoding="utf-8"))
        kb = (ROOT / "docs/knowledge-base/saboryarte.md").read_text(encoding="utf-8")
        for producto in catalogo["productos"] + [catalogo["kit_mini"]]:
            fila = next((l for l in kb.splitlines() if l.startswith(f"| {producto['nombre']} |")), None)
            assert fila, producto["nombre"]
            assert format_price(producto["precio"]) in fila, producto["nombre"]
