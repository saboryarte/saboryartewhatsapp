"""Rutas del artefacto de noviembre de 2026: traspasos directos (regalos, distribuidores, proveedores), quejas,
alianzas, «por ahora no», archivos que no se pueden ver, día de despacho y permiso para la postventa."""
import time
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app import claude
from app.flows import get_step
from app.handoff import COLOMBIA, dispatch_day, priority, spanish_date
from app.pedidos import PAGADO, create_order, get_order, update_order
from app.state import load_conversation
from tests.test_bold import REMITENTE, _mensaje, client, elegir, escribir, mocks  # noqa: F401


def ultimo_texto(mocks) -> str:
    return mocks["text"].call_args.args[1]


def fuera_de_horario(monkeypatch):
    monkeypatch.setattr("app.router.is_open", lambda now: False)


# ── Traspasos directos desde el menú ──────────────────────────────────────────

class TestTraspasosDirectos:
    @pytest.mark.parametrize("opcion, paso, motivo, etiqueta", [
        ("menu_regalo", "R1", "corporativo", "Corporativo"),
        ("menu_distribuir", "D1", "distribuidor", "Distribuidor"),
        ("menu_proveedor", "V1", "proveedor", "Proveedor"),
    ])
    def test_dentro_del_horario(self, client, mocks, opcion, paso, motivo, etiqueta):
        elegir(client, opcion, "list_reply")
        assert ultimo_texto(mocks) == get_step(paso)["texto"]
        conv = load_conversation(REMITENTE)
        assert conv.en_traspaso and conv.traspaso["motivo"] == motivo and etiqueta in conv.etiquetas
        mocks["notify"].assert_called_once()
        mocks["ask"].assert_not_called()

    @pytest.mark.parametrize("opcion, paso", [("menu_regalo", "R1F"), ("menu_distribuir", "D1F"), ("menu_proveedor", "V1F")])
    def test_fuera_de_horario(self, client, mocks, monkeypatch, opcion, paso):
        fuera_de_horario(monkeypatch)
        elegir(client, opcion, "list_reply")
        assert ultimo_texto(mocks) == get_step(paso)["texto"]
        assert load_conversation(REMITENTE).en_traspaso

    def test_el_bot_queda_en_silencio(self, client, mocks):
        elegir(client, "menu_regalo", "list_reply")
        mocks["text"].reset_mock()
        escribir(client, "son 50 regalos para diciembre")
        mocks["text"].assert_not_called()
        mocks["ask"].assert_not_called()

    def test_regalos_en_temporada_tienen_prioridad_alta(self):
        assert priority("corporativo", datetime(2026, 11, 25, 10, tzinfo=COLOMBIA)) == "alta"
        assert priority("corporativo", datetime(2026, 12, 1, 10, tzinfo=COLOMBIA)) == "alta"
        assert priority("corporativo", datetime(2026, 3, 10, 10, tzinfo=COLOMBIA)) == "normal"
        assert priority("distribuidor", datetime(2026, 11, 25, 10, tzinfo=COLOMBIA)) == "normal"
        assert priority("queja_grave", datetime(2026, 3, 10, 10, tzinfo=COLOMBIA)) == "urgente"


# ── Quejas: Q1 → Q2 (grave) o T1/T2 ───────────────────────────────────────────

class TestQuejas:
    @pytest.fixture
    def en_q1(self, client, mocks):
        mocks["ask"].return_value = "IR:queja"
        escribir(client, "hola")
        escribir(client, "me llegó mal el pedido")
        assert ultimo_texto(mocks) == get_step("Q1")["texto"]
        assert load_conversation(REMITENTE).paso == "Q1"
        mocks["notify"].assert_not_called()

    def test_reclamo_grave_pasa_urgente_con_q2(self, client, mocks, monkeypatch, en_q1):
        monkeypatch.setattr("app.router.is_serious_complaint", AsyncMock(return_value=True))
        escribir(client, "el frasco llegó roto, pedido SAB-00012")
        assert ultimo_texto(mocks) == get_step("Q2")["texto"]
        conv = load_conversation(REMITENTE)
        assert conv.traspaso["motivo"] == "queja_grave" and conv.traspaso["prioridad"] == "urgente"
        assert "Urgente" in conv.etiquetas
        asunto, ficha = mocks["notify"].call_args.args
        assert asunto.startswith("[Saboryarte] 🚨 URGENTE · Reclamo grave")
        assert "Lo que contó el cliente: el frasco llegó roto, pedido SAB-00012" in ficha

    def test_reclamo_normal_pasa_con_t1(self, client, mocks, en_q1):
        escribir(client, "el pedido llegó un día tarde")
        assert ultimo_texto(mocks) == get_step("T1")["texto"]
        conv = load_conversation(REMITENTE)
        assert conv.traspaso["motivo"] == "queja" and conv.traspaso["prioridad"] == "normal"
        assert "URGENTE" not in mocks["notify"].call_args.args[0]

    def test_reclamo_normal_fuera_de_horario_usa_t2(self, client, mocks, monkeypatch, en_q1):
        fuera_de_horario(monkeypatch)
        escribir(client, "el pedido llegó un día tarde")
        assert ultimo_texto(mocks) == get_step("T2")["texto"]

    def test_foto_del_problema_es_grave(self, client, mocks, en_q1):
        client.post("/webhook", json=_mensaje({"type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}}))
        assert ultimo_texto(mocks) == get_step("Q2")["texto"]
        assert load_conversation(REMITENTE).traspaso["motivo"] == "queja_grave"

    def test_si_el_clasificador_falla_se_trata_como_grave(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(claude, "_extract", AsyncMock(return_value=None))
        assert asyncio.run(claude.is_serious_complaint("llegó roto")) is True


# ── Alianzas y «por ahora no» ─────────────────────────────────────────────────

class TestRutasEscritas:
    def test_alianza_pide_el_detalle_y_pasa_a_juanita(self, client, mocks):
        mocks["ask"].return_value = "IR:alianza"
        escribir(client, "hola")
        escribir(client, "tengo una feria y quiero invitarlos")
        assert ultimo_texto(mocks) == get_step("O3")["texto"]
        escribir(client, "Feria Sabores de Antioquia, 12 de diciembre en Envigado")
        assert ultimo_texto(mocks) == get_step("T1")["texto"]
        conv = load_conversation(REMITENTE)
        assert conv.traspaso["motivo"] == "alianza" and "Alianzas" in conv.etiquetas
        assert "Lo que contó el cliente: Feria Sabores de Antioquia" in mocks["notify"].call_args.args[1]

    def test_por_ahora_no_se_despide_y_reinicia(self, client, mocks):
        elegir(client, "menu_comprar", "list_reply")
        mocks["ask"].return_value = "IR:no_por_ahora"
        escribir(client, "mejor otro día, gracias")
        assert ultimo_texto(mocks) == get_step("P10")["texto"]
        conv = load_conversation(REMITENTE)
        assert conv.flujo_activo is None and conv.paso is None and not conv.en_traspaso


# ── Archivos que Especia no puede ver (O2) ────────────────────────────────────

class TestArchivosNoSoportados:
    @pytest.mark.parametrize("tipo, inicio", [
        ("audio", "Recibí tu audio 🎙️ Todavía no los puedo escuchar"),
        ("video", "Recibí tu video 🎬 Todavía no los puedo ver"),
        ("sticker", "Recibí tu sticker 😊 Todavía no los puedo ver"),
    ])
    def test_sin_paso_en_curso_muestra_el_menu(self, client, mocks, tipo, inicio):
        client.post("/webhook", json=_mensaje({"type": tipo, tipo: {"id": "m1", "mime_type": "x/y"}}))
        cuerpo, filas = mocks["list"].call_args.args[1], mocks["list"].call_args.args[3]
        assert cuerpo.startswith(inicio) and filas[0]["id"] == "menu_comprar"

    def test_a_mitad_de_un_paso_lo_retoma(self, client, mocks):
        elegir(client, "p1_kits")
        client.post("/webhook", json=_mensaje({"type": "audio", "audio": {"id": "m1", "mime_type": "audio/ogg"}}))
        cuerpo, botones = mocks["buttons"].call_args.args[1:3]
        assert cuerpo.startswith("Recibí tu audio") and "¿Qué prefieres?" in cuerpo
        assert [b["id"] for b in botones] == ["p4_kit_mini", "p4_armar"]
        assert load_conversation(REMITENTE).paso == "P4"


# ── Día de despacho y permiso para la postventa (P15) ─────────────────────────

class TestDespachoYPostventa:
    @pytest.mark.parametrize("pago, despacho", [
        (datetime(2026, 11, 23, 10, tzinfo=COLOMBIA), date(2026, 11, 24)),  # lunes → martes
        (datetime(2026, 11, 24, 9, tzinfo=COLOMBIA), date(2026, 11, 26)),   # martes → jueves
        (datetime(2026, 11, 26, 20, tzinfo=COLOMBIA), date(2026, 12, 1)),   # jueves → martes siguiente
        (datetime(2026, 11, 28, 12, tzinfo=COLOMBIA), date(2026, 12, 1)),   # sábado → martes
    ])
    def test_siguiente_martes_o_jueves(self, pago, despacho):
        assert dispatch_day(pago) == despacho

    def test_fecha_en_espanol(self):
        assert spanish_date(date(2026, 12, 1)) == "martes 1 de diciembre"

    def _pagado(self):
        order = create_order(REMITENTE, "Laura", [{"producto": "sal_de_oro", "cantidad": 1}],
                             {"nombre": "Laura", "direccion": "Cl 1", "ciudad": "Medellín", "telefono": "300"}, 26_000, 9_000)
        order.estado, order.pagado_en = PAGADO, time.time()
        update_order(order)
        return order

    @pytest.mark.parametrize("opcion, valor, paso", [("p15_si", 1, "P15S"), ("p15_no", 0, "P15N")])
    def test_la_respuesta_queda_en_el_pedido(self, client, mocks, opcion, valor, paso):
        order = self._pagado()
        elegir(client, opcion)
        assert get_order(order.id).postventa == valor
        assert ultimo_texto(mocks) == get_step(paso)["texto"]

    def test_sin_pedido_pagado_solo_responde(self, client, mocks):
        elegir(client, "p15_si")
        assert ultimo_texto(mocks) == get_step("P15S")["texto"]
