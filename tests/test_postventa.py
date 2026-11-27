"""Postventa O4: cuándo se envía la plantilla, a quién, y la conversación que sigue con sus botones."""
import asyncio
import time
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app import postventa
from app.flows import get_step
from app.handoff import COLOMBIA
from app.pedidos import PAGADO, create_order, get_order, update_order
from app.state import load_conversation, save_conversation
from tests.test_bold import REMITENTE, _mensaje, client, elegir, escribir, mocks  # noqa: F401


def _pagado(ciudad="Medellín", postventa=1, pagado=datetime(2026, 11, 23, 10, tzinfo=COLOMBIA), nombre="Laura Gómez"):
    order = create_order(REMITENTE, nombre, [{"producto": "sal_de_oro", "cantidad": 1}],
                         {"nombre": nombre, "direccion": "Cl 1", "ciudad": ciudad, "telefono": "300"}, 26_000, 9_000)
    order.estado, order.pagado_en, order.postventa = PAGADO, pagado.timestamp(), postventa
    update_order(order)
    return get_order(order.id)


@pytest.fixture
def plantilla(monkeypatch):
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.postventa.send_template", mock)
    return mock


def tocar_boton_de_plantilla(client, payload: str, texto: str) -> None:
    client.post("/webhook", json=_mensaje({"type": "button", "button": {"payload": payload, "text": texto}}))


def ultimo_texto(mocks) -> str:
    return mocks["text"].call_args.args[1]


# ── Cuándo le toca ────────────────────────────────────────────────────────────

class TestFecha:
    def test_medellin_despacho_mas_2_dias_habiles_mas_3(self):
        # Pagó el lunes 23 → sale el martes 24 → llega el jueves 26 → postventa el domingo 29.
        assert postventa.due_date(_pagado("Medellín")) == date(2026, 11, 29)

    def test_resto_del_pais_despacho_mas_4_dias_habiles_mas_3(self):
        # Sale el martes 24 → 4 días hábiles (sin fin de semana) → lunes 30 → postventa el jueves 3 de diciembre.
        assert postventa.due_date(_pagado("Bogotá")) == date(2026, 12, 3)

    def test_solo_a_quien_acepto_y_todavia_no_la_recibio(self):
        acepto = _pagado()
        _pagado(postventa=0)
        _pagado(postventa=None)
        enviado = _pagado()
        enviado.postventa_enviada_en = time.time()
        update_order(enviado)
        assert [o.id for o in postventa.due_follow_ups(date(2026, 11, 29))] == [acepto.id]

    def test_ni_antes_de_tiempo_ni_demasiado_tarde(self):
        _pagado()
        assert postventa.due_follow_ups(date(2026, 11, 28)) == []
        assert len(postventa.due_follow_ups(date(2026, 12, 6))) == 1   # 7 días de gracia
        assert postventa.due_follow_ups(date(2026, 12, 7)) == []


# ── Envío de la plantilla ─────────────────────────────────────────────────────

class TestEnvio:
    def test_envia_la_plantilla_con_nombre_referencia_y_botones(self, plantilla, mocks):
        order = _pagado()
        assert asyncio.run(postventa.run_daily(date(2026, 11, 29))) == 1
        destino, nombre, idioma, componentes = plantilla.call_args.args
        assert (destino, nombre, idioma) == (REMITENTE, "especia_postventa", "es")
        assert componentes[0]["parameters"] == [{"type": "text", "text": "Laura"}, {"type": "text", "text": order.referencia}]
        assert [c["parameters"][0]["payload"] for c in componentes[1:]] == ["o4_bien", "o4_problema"]
        assert plantilla.call_args.kwargs["readable"] == (
            f"¡Hola, Laura! 🌿 Soy Especia, de Saboryarte. Tu pedido {order.referencia} ya debería estar contigo. "
            "¿Cómo te llegó?")
        assert get_order(order.id).postventa_enviada_en is not None

    def test_no_se_envia_dos_veces(self, plantilla, mocks):
        _pagado()
        asyncio.run(postventa.run_daily(date(2026, 11, 29)))
        assert asyncio.run(postventa.run_daily(date(2026, 11, 30))) == 0
        plantilla.assert_called_once()

    def test_si_meta_la_rechaza_se_reintenta_al_dia_siguiente(self, plantilla, mocks):
        order = _pagado()
        plantilla.return_value = False
        asyncio.run(postventa.run_daily(date(2026, 11, 29)))
        assert get_order(order.id).postventa_enviada_en is None

    def test_se_aplaza_si_juanita_esta_atendiendo(self, plantilla, mocks):
        _pagado()
        conv = load_conversation(REMITENTE)
        conv.en_traspaso = True
        save_conversation(conv)
        assert asyncio.run(postventa.run_daily(date(2026, 11, 29))) == 0
        plantilla.assert_not_called()

    def test_sin_nombre_saluda_de_nuevo(self, plantilla, mocks):
        _pagado(nombre="")
        asyncio.run(postventa.run_daily(date(2026, 11, 29)))
        assert plantilla.call_args.kwargs["readable"].startswith("¡Hola, de nuevo! 🌿")

    def test_definicion_de_la_plantilla_para_meta(self):
        definicion = postventa.template_definition()
        assert definicion["name"] == "especia_postventa" and definicion["category"] == "UTILITY"
        cuerpo, botones = definicion["components"]
        assert "{{1}}" in cuerpo["text"] and "{{2}}" in cuerpo["text"] and "[" not in cuerpo["text"]
        assert [b["text"] for b in botones["buttons"]] == ["Llegó perfecto", "Tuve un problema"]
        assert all(len(b["text"]) <= 25 for b in botones["buttons"])


# ── La conversación después de la plantilla ───────────────────────────────────

class TestRespuestas:
    @pytest.fixture
    def enviada(self, plantilla, mocks):
        order = _pagado()
        asyncio.run(postventa.run_daily(date(2026, 11, 29)))
        return order

    def test_llego_perfecto_pregunta_por_las_mezclas(self, client, mocks, enviada):
        tocar_boton_de_plantilla(client, "o4_bien", "Llegó perfecto")
        texto, botones = mocks["buttons"].call_args.args[1:3]
        assert texto == get_step("O4B")["texto"]
        assert [b["id"] for b in botones] == ["o4b_encanto", "o4b_bien", "o4b_duda"]

    def test_escribir_llego_perfecto_tambien_cuenta(self, client, mocks, enviada):
        escribir(client, "llegó perfecto")
        assert mocks["buttons"].call_args.args[1] == get_step("O4B")["texto"]

    def test_tuve_un_problema_abre_la_queja(self, client, mocks, enviada):
        tocar_boton_de_plantilla(client, "o4_problema", "Tuve un problema")
        assert ultimo_texto(mocks) == get_step("Q1")["texto"]
        assert load_conversation(REMITENTE).paso == "Q1"

    def test_estan_bien_pide_que_mejorar_y_lo_manda_al_equipo(self, client, mocks, enviada):
        tocar_boton_de_plantilla(client, "o4_bien", "Llegó perfecto")
        elegir(client, "o4b_bien")
        assert ultimo_texto(mocks) == get_step("O4M")["texto"]
        escribir(client, "la sal de sol me pareció muy salada")
        asunto, ficha = mocks["notify"].call_args.args
        assert "Qué mejorar" in asunto and "Laura Gómez" in asunto
        assert "Lo que escribió el cliente: la sal de sol me pareció muy salada" in ficha and enviada.referencia in ficha
        assert ultimo_texto(mocks) == get_step("O4F")["texto"]
        assert load_conversation(REMITENTE).paso is None

    def test_me_encantaron_y_testimonio(self, client, mocks, enviada):
        tocar_boton_de_plantilla(client, "o4_bien", "Llegó perfecto")
        elegir(client, "o4b_encanto")
        assert mocks["buttons"].call_args.args[1] == get_step("O4C")["texto"]
        elegir(client, "o4c_si")
        assert get_order(enviada.id).testimonio == 1
        assert ultimo_texto(mocks) == get_step("O4S")["texto"]
        escribir(client, "La sal de oro le cambió la vida a mis huevos")
        asunto, ficha = mocks["notify"].call_args.args
        assert "Testimonio" in asunto and "La sal de oro le cambió la vida a mis huevos" in ficha
        assert ultimo_texto(mocks) == get_step("O4F")["texto"]

    def test_listo_gracias_cierra(self, client, mocks, enviada):
        elegir(client, "o4c_listo")
        assert ultimo_texto(mocks) == get_step("O4F")["texto"]
        mocks["notify"].assert_not_called()

    def test_pedir_de_nuevo_abre_la_compra(self, client, mocks, enviada):
        elegir(client, "o4c_pedir")
        assert [b["id"] for b in mocks["buttons"].call_args.args[2]] == ["p1_kits", "p1_pedido"]

    def test_tengo_una_duda_pasa_a_juanita(self, client, mocks, enviada):
        elegir(client, "o4b_duda")
        assert load_conversation(REMITENTE).en_traspaso
        mocks["notify"].assert_called_once()
