"""Archivo de chats (60 días), memoria del cliente que vuelve y chat de cada pedido pagado (1 año)."""
import time

import pytest

from app import archivo
from app.state import load_conversation, save_conversation
from tests.test_bold import LINK, REMITENTE, armar_y_confirmar, aviso_bold, bold_pruebas, client, elegir, escribir, mocks  # noqa: F401

DIA = 24 * 3600


def _chat(remitente=REMITENTE):
    return [(q, t) for q, t, _ in archivo.last_session(remitente, time.time())]


def _nueva_sesion():
    """Pasa más de 24 h: la conversación activa caduca, pero la memoria del cliente sigue."""
    conv = load_conversation(REMITENTE)
    conv.reset_flow()
    conv.historial = []
    conv.opciones = None
    save_conversation(conv)


class TestArchivoDeChats:
    def test_guarda_lo_que_escribe_y_toca_el_cliente(self, client, mocks):
        escribir(client, "hola")
        elegir(client, "menu_comprar", "list_reply")
        chat = _chat()
        assert ("cliente", "hola") in chat and ("cliente", "[Tocó] menu_comprar") in chat

    def test_texto_legible_de_lo_que_envia_especia(self):
        lista = {"type": "interactive", "interactive": {"type": "list", "body": {"text": "Elige"}, "action": {
            "button": "Ver mezclas", "sections": [{"title": "x", "rows": [{"id": "a", "title": "Sal de oro"}, {"id": "b", "title": "Sal de sol"}]}]}}}
        botones = {"type": "interactive", "interactive": {"type": "button", "body": {"text": "¿Todo bien?"}, "action": {
            "buttons": [{"type": "reply", "reply": {"id": "s", "title": "Sí"}}, {"type": "reply", "reply": {"id": "n", "title": "No"}}]}}}
        assert archivo.outgoing_text({"type": "text", "text": {"body": "Hola"}}) == "Hola"
        assert archivo.outgoing_text(lista) == "Elige\n[Lista «Ver mezclas»: Sal de oro / Sal de sol]"
        assert archivo.outgoing_text(botones) == "¿Todo bien?\n[Botones: Sí / No]"

    def test_la_sesion_se_corta_en_una_pausa_de_mas_de_24_h(self, monkeypatch):
        ahora = time.time()
        for desfase, texto in ((3 * DIA, "pedido viejo"), (60, "hola de nuevo"), (30, "quiero un kit")):
            monkeypatch.setattr(archivo.time, "time", lambda d=desfase: ahora - d)
            archivo.record(REMITENTE, archivo.CLIENTE, texto)
        assert [t for _, t, _ in archivo.last_session(REMITENTE, ahora)] == ["hola de nuevo", "quiero un kit"]


class TestClienteQueVuelve:
    def test_ofrece_los_mismos_datos_de_envio(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        _nueva_sesion()
        elegir(client, "p1_pedido", "list_reply")
        escribir(client, "2 de oro")
        cuerpo, botones = mocks["buttons"].call_args.args[1:3]
        assert "mismos datos" in cuerpo and "Cra 43 # 10-20, Envigado" in cuerpo and "Tel. 3001112233" in cuerpo
        assert [b["id"] for b in botones] == ["p7g_misma", "p7g_otra"]
        elegir(client, "p7g_misma")
        resumen = mocks["buttons"].call_args.args[1]
        assert "Envío a: Laura Gómez, Cra 43 # 10-20, Envigado" in resumen
        assert load_conversation(REMITENTE).paso == "P8"

    def test_otros_datos_pide_el_envio_desde_cero(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        _nueva_sesion()
        elegir(client, "p1_pedido", "list_reply")
        escribir(client, "2 de oro")
        elegir(client, "p7g_otra")
        conv = load_conversation(REMITENTE)
        assert conv.paso == "P7" and "envio" not in conv.datos and "envio_guardado" not in conv.datos

    def test_cliente_nuevo_no_ve_la_pregunta(self, client, mocks):
        elegir(client, "p1_pedido", "list_reply")
        escribir(client, "2 de oro")
        assert load_conversation(REMITENTE).paso == "P7"

    def test_nota_para_claude_sin_direccion_ni_telefono(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        aviso_bold(client, "SALE_APPROVED")
        nota = archivo.client_note(REMITENTE)
        assert "SAB-00001" in nota and "pagado" in nota and "2 × Sal de oro" in nota and "Envigado" in nota
        assert "Cra 43" not in nota and "3001112233" not in nota
        assert archivo.client_note("573009999999") is None


class TestChatDelPedidoPagado:
    def test_guarda_ficha_y_chat_en_un_archivo_legible(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        aviso_bold(client, "SALE_APPROVED")
        archivos = list(archivo.pedidos_dir().glob("*SAB-00001*.txt"))
        assert len(archivos) == 1 and "Laura" in archivos[0].name
        texto = archivos[0].read_text(encoding="utf-8")
        assert "Pedido SAB-00001 · estado: Pagado" in texto
        assert "Laura: 2 de oro" in texto and "Laura: [Tocó] p8_confirmar" in texto

    def test_un_pago_rechazado_no_guarda_el_chat(self, client, mocks, bold_pruebas):
        armar_y_confirmar(client)
        aviso_bold(client, "SALE_REJECTED")
        assert not list(archivo.pedidos_dir().glob("*.txt"))


class TestRetencion:
    def _en(self, monkeypatch, cuando):
        monkeypatch.setattr(archivo.time, "time", lambda: cuando)

    def test_borra_chats_de_mas_de_60_dias(self, monkeypatch):
        ahora = time.time()
        self._en(monkeypatch, ahora - 61 * DIA)
        archivo.record(REMITENTE, archivo.CLIENTE, "viejo")
        self._en(monkeypatch, ahora - 59 * DIA)
        archivo.record(REMITENTE, archivo.CLIENTE, "reciente")
        assert archivo.purge(ahora)["chat"] == 1
        assert [t for _, t, _ in archivo.last_session(REMITENTE, ahora)] == ["reciente"]

    def test_cliente_sin_pago_se_borra_a_los_60_dias(self, monkeypatch):
        ahora = time.time()
        self._en(monkeypatch, ahora - 61 * DIA)
        archivo.remember_order(REMITENTE, "Laura", {"ciudad": "Envigado"}, "SAB-00001", "1 × Kit mini")
        archivo.purge(ahora)
        assert archivo.saved_shipping(REMITENTE) is None

    def test_cliente_con_pago_se_guarda_un_ano(self, monkeypatch):
        ahora = time.time()
        self._en(monkeypatch, ahora - 200 * DIA)
        archivo.remember_order(REMITENTE, "Laura", {"ciudad": "Envigado"}, "SAB-00001", "1 × Kit mini")
        archivo.remember_payment(REMITENTE, "SAB-00001")
        archivo.purge(ahora)
        assert archivo.saved_shipping(REMITENTE) == {"ciudad": "Envigado"}
        archivo.purge(ahora + 166 * DIA)
        assert archivo.saved_shipping(REMITENTE) is None

    def test_chat_de_pedido_y_su_archivo_se_borran_al_ano(self, monkeypatch):
        ahora = time.time()
        self._en(monkeypatch, ahora - 366 * DIA)
        ruta = archivo.save_order_chat(1, "SAB-00001", REMITENTE, "Laura", "ficha")
        assert ruta.exists()
        assert archivo.purge(ahora)["chats_pedidos"] == 1
        assert not ruta.exists()
