"""Ruta de compra: navegación (P1, P4, P6) y prompt de Claude con la base de conocimiento."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.flows import format_price, get_step, list_rows, orderable_products
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


class TestNavegacionCompra:
    def test_comprar_muestra_p1_con_dos_botones(self, client, mocks):
        elegir(client, "menu_comprar")
        _, cuerpo, botones = mocks["buttons"].call_args.args
        assert cuerpo == get_step("P1")["texto"]
        assert [(b["id"], b["title"]) for b in botones] == [("p1_kits", "Kits"), ("p1_pedido", "Hacer mi pedido")]
        conv = load_conversation(REMITENTE)
        assert (conv.flujo_activo, conv.paso) == ("compra", "P1")

    def test_kits_muestra_el_precio_del_kit_mini(self, client, mocks):
        elegir(client, "p1_kits", "button_reply")
        _, cuerpo, botones = mocks["buttons"].call_args.args
        assert "$60.000" in cuerpo and "[precio" not in cuerpo
        assert ids(botones) == ["p4_kit_mini", "p4_armar"]

    def test_armar_kit_lista_los_tamanos_con_precio(self, client, mocks):
        elegir(client, "p4_armar", "button_reply")
        filas = mocks["list"].call_args.args[3]
        assert ids(filas) == ["kit_2", "kit_3", "kit_4", "kit_5", "kit_6"]
        assert filas[0]["title"] == "2 mezclas · $48.000"

    def test_el_precio_de_los_kits_sale_de_la_hoja_de_precios(self, client, mocks, monkeypatch):
        monkeypatch.setattr("app.flows.kit_price", lambda n: 50_000 if n == 2 else 1)
        elegir(client, "p4_armar", "button_reply")
        assert mocks["list"].call_args.args[3][0]["title"] == "2 mezclas · $50.000"

    def test_hacer_mi_pedido_lista_las_mezclas_y_el_precio(self, client, mocks):
        elegir(client, "p1_pedido", "button_reply")
        texto = mocks["text"].call_args.args[1]
        assert "$26.000" in texto and "• Sal chilemon (100 g)" in texto and "• Mezcla tierra de fuego (60 g)" in texto
        assert "[mezclas]" not in texto and "[precio]" not in texto
        assert load_conversation(REMITENTE).paso == "P6"

    def test_opcion_de_una_lista_vieja_muestra_el_menu(self, client, mocks):
        elegir(client, "p1_catalogo")  # ya no existe
        assert ids(mocks["list"].call_args.args[3])[0] == "menu_comprar"


class TestPromptDeClaude:
    def test_incluye_la_base_publica(self):
        prompt = system_prompt()
        assert "<base_de_conocimiento>" in prompt
        assert "Sal de luna rosa" in prompt and "$26.000" in prompt and "$18.000" in prompt

    def test_no_incluye_informacion_interna(self):
        prompt = system_prompt()
        assert "18.500" not in prompt and "160 por gramo" not in prompt

    def test_la_base_de_conocimiento_no_tiene_precios_escritos_a_mano(self):
        # Los precios salen de precios.yaml; uno escrito a mano en la base podría no coincidir con lo que se cobra.
        kb = (ROOT / "contenido/base_de_conocimiento.md").read_text(encoding="utf-8")
        assert "$" not in kb
        for marca in ("{{tabla_productos}}", "{{tabla_kits}}", "{{tabla_envios}}"):
            assert marca + "\n" in kb, marca

    def test_el_prompt_trae_los_precios_que_se_cobran(self):
        prompt = system_prompt()
        assert not [l for l in prompt.splitlines() if l.strip().startswith("{{tabla")]
        for producto in orderable_products():
            fila = next((l for l in prompt.splitlines() if l.startswith(f"| {producto['nombre']} |")), None)
            assert fila and fila.endswith(f"| {format_price(producto['precio'])} |"), producto["nombre"]
        assert "| Precio | $48.000 | $70.000 | $88.000 | $110.000 | $133.000 |" in prompt
        assert "| Medellín | $9.000 |" in prompt

    def test_incluye_la_voz_para_whatsapp_y_no_el_manual_completo(self):
        prompt = system_prompt()
        assert "<voz_de_marca>" in prompt and "💛" in prompt
        assert "@tropical_spice_co" not in prompt and "hashtags propios" not in prompt
