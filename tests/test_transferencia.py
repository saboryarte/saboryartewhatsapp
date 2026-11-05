"""Pago por transferencia: datos de la cuenta, comprobante del cliente y botones del equipo en el correo."""
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openpyxl import load_workbook

from app import exporte, transferencias
from app.config import settings
from app.handoff import COLOMBIA
from app.pedidos import ANULADO, ESPERANDO_COMPROBANTE, PAGADO, POR_VERIFICAR, TRANSFERENCIA, get_order
from tests.test_bold import (  # noqa: F401
    LINK, REMITENTE, _mensaje, aviso_bold, bold_pruebas, client, elegir, escribir, mocks,
)


@pytest.fixture
def botones(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://bot.saboryarte.test")
    monkeypatch.setattr(settings, "transfer_link_secret", "secreto-de-prueba")


@pytest.fixture
def descarga(monkeypatch):
    m = AsyncMock(return_value=(b"\xff\xd8 foto del comprobante", "image/jpeg"))
    monkeypatch.setattr("app.router.download_media", m)
    return m


def armar_y_elegir_transferencia(client):
    elegir(client, "p1_pedido", "list_reply")
    escribir(client, "2 de oro")
    escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
    elegir(client, "p8_transferencia")


def enviar_foto(client, mime="image/jpeg", tipo="image"):
    client.post("/webhook", json=_mensaje({"type": tipo, tipo: {"id": "media-1", "mime_type": mime}}))


def _enlace(mocks, accion):
    _, kwargs = mocks["notify"].call_args
    url = dict(kwargs["buttons"])[transferencias.ACCIONES[accion]]
    path, token = re.match(r"https://bot\.saboryarte\.test(/transferencias/\d+/\w+)\?t=(\w+)", url).groups()
    return path, token


class TestElegirTransferencia:
    def test_el_resumen_ofrece_los_dos_medios(self, client, mocks):
        elegir(client, "p1_pedido", "list_reply")
        escribir(client, "2 de oro")
        escribir(client, "Laura Gómez, Cra 43 # 10-20, Envigado, 3001112233")
        titulos = [b["title"] for b in mocks["buttons"].call_args.args[2]]
        assert titulos == ["Pagar con Bold", "Transferencia", "Cambiar algo"]

    def test_da_los_datos_de_la_cuenta_y_el_total(self, client, mocks):
        armar_y_elegir_transferencia(client)
        texto = mocks["text"].call_args.args[1]
        assert "*SAB-00001*" in texto and "*$64.000*" in texto and "693 00005720" in texto and "Bancolombia" in texto
        assert "comprobante" in texto
        pedido = get_order(1)
        assert pedido.medio_pago == TRANSFERENCIA and pedido.estado == ESPERANDO_COMPROBANTE
        mocks["crear_link"].assert_not_called()

    def test_cambiar_a_bold_anula_la_transferencia_sin_comprobante(self, client, mocks, bold_pruebas):
        armar_y_elegir_transferencia(client)
        elegir(client, "p8_confirmar")
        assert get_order(1).estado == ANULADO and get_order(2).medio_pago == "bold"


class TestComprobante:
    def test_guarda_el_comprobante_y_avisa_al_equipo_con_botones(self, client, mocks, descarga, botones):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        pedido = get_order(1)
        assert pedido.estado == POR_VERIFICAR and Path(pedido.comprobante).read_bytes().startswith(b"\xff\xd8")
        assert "Comprobantes" in pedido.comprobante and pedido.comprobante.endswith("SAB-00001.jpg")
        assert "Recibimos tu comprobante" in mocks["text"].call_args.args[1]
        asunto = mocks["notify"].call_args.args[0]
        assert "Verificar transferencia" in asunto and "$64.000" in asunto
        _, kwargs = mocks["notify"].call_args
        assert kwargs["attachments"] == [Path(pedido.comprobante)]
        assert [label for label, _ in kwargs["buttons"]] == ["Confirmar pago", "No llegó"]

    def test_sin_botones_explica_como_confirmar(self, client, mocks, descarga):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        _, kwargs = mocks["notify"].call_args
        assert kwargs["buttons"] == [] and "app.transferencias confirmar SAB-00001" in mocks["notify"].call_args.args[1]

    def test_acepta_pdf(self, client, mocks, descarga):
        descarga.return_value = (b"%PDF-1.4", "application/pdf")
        armar_y_elegir_transferencia(client)
        enviar_foto(client, "application/pdf", "document")
        assert get_order(1).comprobante.endswith(".pdf")

    def test_rechaza_otros_tipos_de_archivo(self, client, mocks, descarga):
        armar_y_elegir_transferencia(client)
        enviar_foto(client, "application/zip", "document")
        assert "foto" in mocks["text"].call_args.args[1] and get_order(1).estado == ESPERANDO_COMPROBANTE
        descarga.assert_not_called()

    def test_archivo_sin_pedido_pendiente(self, client, mocks, descarga):
        enviar_foto(client)
        assert "no veo ninguno pendiente" in mocks["text"].call_args.args[1]
        descarga.assert_not_called()


class TestBotonesDelEquipo:
    def test_abrir_el_enlace_no_confirma_nada(self, client, mocks, descarga, botones):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        path, token = _enlace(mocks, transferencias.CONFIRMAR)
        pagina = client.get(f"{path}?t={token}")
        assert pagina.status_code == 200 and "$64.000" in pagina.text and "<form" in pagina.text
        assert get_order(1).estado == POR_VERIFICAR

    def test_confirmar_deja_el_pedido_pagado_y_avisa_al_cliente(self, client, mocks, descarga, botones):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        path, token = _enlace(mocks, transferencias.CONFIRMAR)
        respuesta = client.post(path, content=f"t={token}", headers={"content-type": "application/x-www-form-urlencoded"})
        assert "quedó pagado" in respuesta.text
        pedido = get_order(1)
        assert pedido.estado == PAGADO and pedido.pagado_en
        assert "Recibimos tu pago" in mocks["text"].call_args.args[1]
        assert "Pedido pagado SAB-00001" in mocks["notify"].call_args.args[0]
        otra_vez = client.post(path, content=f"t={token}", headers={"content-type": "application/x-www-form-urlencoded"})
        assert "ya estaba confirmado" in otra_vez.text

    def test_no_llego_pide_el_comprobante_otra_vez(self, client, mocks, descarga, botones):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        path, token = _enlace(mocks, transferencias.RECHAZAR)
        client.post(path, content=f"t={token}", headers={"content-type": "application/x-www-form-urlencoded"})
        assert get_order(1).estado == ESPERANDO_COMPROBANTE
        assert "Todavía no vemos la transferencia" in mocks["text"].call_args.args[1]

    def test_token_falso_o_de_otra_accion(self, client, mocks, descarga, botones):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        _, token_rechazar = _enlace(mocks, transferencias.RECHAZAR)
        assert client.get("/transferencias/1/confirmar?t=falso").status_code == 403
        r = client.post("/transferencias/1/confirmar", content=f"t={token_rechazar}",
                        headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 403 and get_order(1).estado == POR_VERIFICAR

    def test_sin_llave_ningun_token_sirve(self, client, mocks, descarga, monkeypatch):
        monkeypatch.setattr(settings, "transfer_link_secret", "")
        assert not transferencias.valid_token(1, "confirmar", transferencias._token(1, "confirmar"))


class TestExporteDiario:
    def test_excel_del_dia_y_del_mes(self, client, mocks, descarga, botones, bold_pruebas):
        armar_y_elegir_transferencia(client)
        enviar_foto(client)
        path, token = _enlace(mocks, transferencias.CONFIRMAR)
        client.post(path, content=f"t={token}", headers={"content-type": "application/x-www-form-urlencoded"})
        hoy = datetime.now(tz=COLOMBIA).date()
        ruta, pedidos = exporte.build_export(hoy)
        assert len(pedidos) == 1 and ruta.name == f"Pedidos {hoy.isoformat()}.xlsx"
        libro = load_workbook(ruta)
        dia = list(libro.worksheets[0].iter_rows(values_only=True))
        assert dia[0][:4] == ("Referencia", "Fecha", "Cliente", "WhatsApp")
        assert dia[1][0] == "SAB-00001" and dia[1][4] == "2 × Sal de oro" and dia[1][7] == 64000
        assert dia[1][8] == "Transferencia" and dia[1][9] == "Pagado" and dia[1][13] == "Envigado"
        assert len(libro.worksheets) == 2
        asunto, cuerpo = exporte.summary(hoy, pedidos)
        assert "1 pedido(s), 1 pagado(s) por $64.000" in asunto and "SAB-00001" in cuerpo

    def test_dia_sin_pedidos(self):
        asunto, cuerpo = exporte.summary(datetime(2026, 11, 4).date(), [])
        assert "sin pedidos" in asunto and "no hubo pedidos" in cuerpo

    def test_envia_el_excel_adjunto(self, monkeypatch):
        aviso = AsyncMock(return_value=True)
        monkeypatch.setattr(exporte, "notify_team", aviso)
        assert exporte.daily(datetime(2026, 11, 4).date()) == 0
        assert aviso.call_args.kwargs["attachments"][0].name == "Pedidos 2026-11-04.xlsx"
