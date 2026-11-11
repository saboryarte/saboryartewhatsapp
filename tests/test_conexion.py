"""Conexión del número del negocio en coexistencia y firma de los avisos de Meta."""
import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import conexion
from app.config import apply_connection, conexion_path, settings
from app.main import app

SECRETO_APP = "secreto-de-la-app"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_db_path", str(tmp_path / "conversaciones.db"))
    monkeypatch.setattr(settings, "public_base_url", "https://bot.saboryarte.test")
    monkeypatch.setattr(settings, "transfer_link_secret", "secreto-de-enlaces")
    monkeypatch.setattr(settings, "meta_app_id", "1234567890")
    monkeypatch.setattr(settings, "meta_signup_config_id", "cfg-987")
    # connect() cambia el número y la llave del bot: monkeypatch los restaura al terminar.
    monkeypatch.setattr(settings, "whatsapp_token", "llave-de-prueba")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "numero-de-prueba")
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr("app.conexion.notify_team", notify)
    return notify


def _enlace():
    url = conexion.signup_link()
    query = dict(p.split("=") for p in url.split("?")[1].split("&"))
    return int(query["exp"]), query["t"]


def _meta_falso(monkeypatch, numeros=None):
    """Simula las respuestas de Meta y guarda las llamadas."""
    numeros = numeros if numeros is not None else [
        {"id": "PN1", "display_phone_number": "+57 300 000 0000", "verified_name": "Saboryarte", "is_on_biz_app": True,
         "platform_type": "CLOUD_API"}]
    llamadas = []

    async def graph(method, path, token=None, **kwargs):
        llamadas.append((method, path.split("?")[0], token, kwargs))
        if path == "oauth/access_token":
            return {"access_token": "llave-del-negocio", "token_type": "bearer"}
        if path == "debug_token":
            return {"data": {"granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": ["WABA9"]}]}}
        if path.endswith("/phone_numbers"):
            return {"data": numeros}
        if path.endswith("/subscribed_apps"):
            return {"success": True}
        if path.endswith("/smb_app_data"):
            return {"messaging_product": "whatsapp", "request_id": f"req-{kwargs['json']['sync_type']}"}
        raise AssertionError(f"llamada inesperada {method} {path}")

    monkeypatch.setattr("app.conexion._graph", graph)
    return llamadas


# ── Firma de los avisos de WhatsApp ───────────────────────────────────────────

class TestFirmaDeMeta:
    CUERPO = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()

    @pytest.fixture(autouse=True)
    def llave(self, monkeypatch):
        monkeypatch.setattr(settings, "app_secret", SECRETO_APP)

    def _post(self, client, firma=None):
        headers = {"Content-Type": "application/json"}
        if firma:
            headers["X-Hub-Signature-256"] = firma
        return client.post("/webhook", content=self.CUERPO, headers=headers)

    def test_aviso_bien_firmado_se_acepta(self, client):
        firma = "sha256=" + hmac.new(SECRETO_APP.encode(), self.CUERPO, hashlib.sha256).hexdigest()
        assert self._post(client, firma).status_code == 200

    def test_aviso_sin_firma_se_rechaza(self, client):
        assert self._post(client).status_code == 403

    def test_aviso_con_firma_falsa_se_rechaza(self, client):
        firma = "sha256=" + hmac.new(b"otra-llave", self.CUERPO, hashlib.sha256).hexdigest()
        assert self._post(client, firma).status_code == 403


# ── Enlace y página de conexión ───────────────────────────────────────────────

class TestEnlace:
    def test_el_enlace_es_valido_hasta_que_vence(self, entorno):
        exp, t = _enlace()
        assert conexion.valid_link(exp, t)
        assert exp - time.time() == pytest.approx(conexion.LINK_HOURS * 3600, abs=5)

    def test_un_enlace_vencido_o_alterado_no_sirve(self, entorno):
        exp, t = _enlace()
        assert not conexion.valid_link(exp + 1, t)
        vencido = int(time.time()) - 10
        assert not conexion.valid_link(vencido, conexion._firma(vencido))

    def test_sin_llave_no_hay_enlace(self, entorno, monkeypatch):
        monkeypatch.setattr(settings, "transfer_link_secret", "")
        with pytest.raises(conexion.ConexionError):
            conexion.signup_link()
        assert not conexion.valid_link(int(time.time()) + 100, "")

    def test_la_pagina_lanza_el_registro_en_coexistencia(self, client, entorno):
        exp, t = _enlace()
        r = client.get(f"/conectar?exp={exp}&t={t}")
        assert r.status_code == 200
        assert "whatsapp_business_app_onboarding" in r.text and "cfg-987" in r.text and "1234567890" in r.text

    def test_la_pagina_con_enlace_falso_no_se_muestra(self, client, entorno):
        r = client.get(f"/conectar?exp={int(time.time()) + 100}&t=falso")
        assert r.status_code == 403 and "FB.login" not in r.text

    def test_sin_ids_de_meta_la_pagina_lo_dice(self, client, entorno, monkeypatch):
        monkeypatch.setattr(settings, "meta_signup_config_id", "")
        exp, t = _enlace()
        assert "META_SIGNUP_CONFIG_ID" in client.get(f"/conectar?exp={exp}&t={t}").text


# ── Terminar la conexión ──────────────────────────────────────────────────────

class TestConectar:
    @pytest.fixture(autouse=True)
    def llave(self, monkeypatch):
        # El código se cambia por la llave con la llave secreta de la app (aquí no se envían avisos firmados).
        monkeypatch.setattr(settings, "app_secret", SECRETO_APP)

    def test_conecta_el_numero_y_el_bot_empieza_a_usarlo(self, client, entorno, monkeypatch):
        llamadas = _meta_falso(monkeypatch)
        exp, t = _enlace()
        r = client.post("/conectar", json={"code": "codigo-1", "waba_id": "WABA1", "phone_number_id": "", "exp": exp, "t": t})

        assert r.status_code == 200 and r.json()["ok"]
        assert "+57 300 000 0000" in r.json()["mensaje"]
        rutas = [(m, p) for m, p, _, _ in llamadas]
        assert ("POST", "WABA1/subscribed_apps") in rutas
        assert not any(p.endswith("/register") for _, p in rutas)  # en coexistencia el número ya está registrado
        assert settings.whatsapp_token == "llave-del-negocio" and settings.whatsapp_phone_number_id == "PN1"

        datos = json.loads(conexion_path().read_text())
        assert datos["waba_id"] == "WABA1" and datos["en_app"] is True
        assert datos["sincronizacion"]["historial"] == "pedida (request_id req-history)"
        assert datos["sincronizacion"]["contactos"] == "pedida (request_id req-smb_app_state_sync)"
        assert oct(os.stat(conexion_path()).st_mode & 0o777) == "0o600"
        asunto = entorno.call_args.args[0]
        assert "conectado" in asunto and "llave-del-negocio" not in entorno.call_args.args[1]

    def test_sin_cuenta_en_la_sesion_la_busca_en_la_llave(self, entorno, monkeypatch):
        import asyncio
        llamadas = _meta_falso(monkeypatch)
        asyncio.run(conexion.connect("codigo-2"))
        assert ("POST", "WABA9/subscribed_apps") in [(m, p) for m, p, _, _ in llamadas]

    def test_avisa_si_el_numero_no_esta_en_la_app(self, entorno, monkeypatch):
        import asyncio
        _meta_falso(monkeypatch, numeros=[{"id": "PN2", "display_phone_number": "+57 1", "is_on_biz_app": False}])
        asyncio.run(conexion.connect("codigo-3", "WABA1"))
        assert "no reporta este número como activo" in entorno.call_args.args[1]

    def test_si_falla_la_sincronizacion_igual_queda_conectado(self, entorno, monkeypatch):
        import asyncio
        _meta_falso(monkeypatch)
        original = conexion._graph

        async def graph(method, path, token=None, **kwargs):
            if path.endswith("/smb_app_data"):
                raise conexion.ConexionError("ya se sincronizó")
            return await original(method, path, token, **kwargs)

        monkeypatch.setattr("app.conexion._graph", graph)
        asyncio.run(conexion.connect("codigo-4", "WABA1"))
        assert json.loads(conexion_path().read_text())["sincronizacion"]["historial"].startswith("falló")

    def test_enlace_falso_no_conecta(self, client, entorno, monkeypatch):
        llamadas = _meta_falso(monkeypatch)
        r = client.post("/conectar", json={"code": "c", "exp": int(time.time()) + 100, "t": "falso"})
        assert r.status_code == 403 and llamadas == []

    def test_error_de_meta_se_muestra_en_la_pagina(self, client, entorno, monkeypatch):
        monkeypatch.setattr("app.conexion.connect", AsyncMock(side_effect=conexion.ConexionError("código vencido")))
        exp, t = _enlace()
        r = client.post("/conectar", json={"code": "c", "exp": exp, "t": t})
        assert r.status_code == 502 and "código vencido" in r.json()["mensaje"]

    def test_al_reiniciar_el_bot_usa_el_numero_conectado(self, entorno, monkeypatch):
        conexion._save({"token": "llave-guardada", "phone_number_id": "PN7", "waba_id": "W"})
        assert apply_connection()["waba_id"] == "W"
        assert settings.whatsapp_token == "llave-guardada" and settings.whatsapp_phone_number_id == "PN7"


# ── Avisos de Meta sobre la conexión ─────────────────────────────────────────

def _aviso(field: str, value: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{"field": field, "value": value}]}]}


class TestAvisosDeConexion:
    def test_desconexion_por_inactividad_avisa_al_equipo(self, client, entorno):
        r = client.post("/webhook", json=_aviso("account_update", {
            "event": "PARTNER_REMOVED", "disconnection_info": {"reason": "PRIMARY_INACTIVITY", "initiated_by": "SYSTEM"}}))
        assert r.status_code == 200
        asunto, cuerpo = entorno.call_args.args
        assert "desconectado" in asunto and "14 días" in cuerpo

    def test_otros_avisos_de_cuenta_no_alertan(self, client, entorno):
        client.post("/webhook", json=_aviso("account_update", {"event": "ACCOUNT_RECONNECTED"}))
        entorno.assert_not_called()

    def test_historial_y_contactos_no_hacen_responder_al_bot(self, client, entorno, monkeypatch):
        ask = AsyncMock()
        monkeypatch.setattr("app.router.ask_claude", ask)
        historial = {"messaging_product": "whatsapp", "history": [{
            "metadata": {"phase": 0, "chunk_order": 1, "progress": 100},
            "threads": [{"id": "573001112233", "messages": [
                {"from": "573001112233", "id": "wamid.h1", "timestamp": "1", "type": "text", "text": {"body": "hola"},
                 "history_context": {"status": "READ"}}]}]}]}
        assert client.post("/webhook", json=_aviso("history", historial)).status_code == 200
        contactos = {"messaging_product": "whatsapp", "state_sync": [
            {"type": "contact", "action": "add", "contact": {"full_name": "Laura", "phone_number": "573001112233"}}]}
        assert client.post("/webhook", json=_aviso("smb_app_state_sync", contactos)).status_code == 200
        ask.assert_not_called()
        entorno.assert_not_called()
