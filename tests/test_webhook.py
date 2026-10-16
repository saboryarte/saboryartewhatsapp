import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from app.main import app

VALID_TOKEN = "mi-token-secreto"
CHALLENGE = "abc123"


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("VERIFY_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("WHATSAPP_TOKEN", "dummy")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")


@pytest.fixture(autouse=True)
def mock_externos(monkeypatch):
    monkeypatch.setattr("app.main.ask_claude", AsyncMock(return_value="respuesta de Claude"))
    monkeypatch.setattr("app.main.send_text", AsyncMock(return_value=None))


@pytest.fixture
def client():
    return TestClient(app)


# ── GET /webhook ──────────────────────────────────────────────────────────────

class TestVerifyWebhook:
    def test_verificacion_exitosa(self, client, monkeypatch):
        monkeypatch.setattr("app.main.settings.verify_token", VALID_TOKEN)
        resp = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VALID_TOKEN,
                "hub.challenge": CHALLENGE,
            },
        )
        assert resp.status_code == 200
        assert resp.text == CHALLENGE

    def test_token_incorrecto_devuelve_403(self, client, monkeypatch):
        monkeypatch.setattr("app.main.settings.verify_token", VALID_TOKEN)
        resp = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token-equivocado",
                "hub.challenge": CHALLENGE,
            },
        )
        assert resp.status_code == 403

    def test_modo_invalido_devuelve_400(self, client, monkeypatch):
        monkeypatch.setattr("app.main.settings.verify_token", VALID_TOKEN)
        resp = client.get(
            "/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": VALID_TOKEN,
                "hub.challenge": CHALLENGE,
            },
        )
        assert resp.status_code == 400

    def test_parametros_faltantes_devuelven_403(self, client):
        resp = client.get("/webhook")
        assert resp.status_code == 403


# ── POST /webhook ─────────────────────────────────────────────────────────────

TEXT_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "entry-1",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "1234567890", "phone_number_id": "phone-1"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": "521234567890"}],
                        "messages": [
                            {
                                "from": "521234567890",
                                "id": "msg-1",
                                "timestamp": "1700000000",
                                "type": "text",
                                "text": {"body": "Hola mundo"},
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

# Payload BSUID: from_user_id en el mensaje, user_id en el contacto (sin from / wa_id)
BSUID_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "entry-bsuid",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "1234567890", "phone_number_id": "phone-1"},
                        "contacts": [{"profile": {"name": "BSUID User"}, "user_id": "bsuid_111222333"}],
                        "messages": [
                            {
                                "from_user_id": "bsuid_111222333",
                                "id": "wamid.bsuid1",
                                "timestamp": "1700000000",
                                "type": "text",
                                "text": {"body": "Hola desde BSUID"},
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


class TestReceiveWebhook:
    def test_mensaje_texto_devuelve_200(self, client):
        resp = client.post("/webhook", json=TEXT_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_objeto_invalido_devuelve_400(self, client):
        bad = {**TEXT_PAYLOAD, "object": "page"}
        resp = client.post("/webhook", json=bad)
        assert resp.status_code == 400

    def test_actualizacion_estado_devuelve_200(self, client, monkeypatch):
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "entry-1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                                "statuses": [
                                    {"id": "msg-1", "status": "delivered", "timestamp": "1700000001", "recipient_id": "521234567890"}
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_cuerpo_invalido_devuelve_422(self, client):
        resp = client.post("/webhook", json={"object": "whatsapp_business_account"})
        assert resp.status_code == 422

    def test_mensaje_sistema_sin_from_devuelve_200(self, client):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "System"}}],
                "messages": [{
                    "id": "wamid.sys1",
                    "timestamp": "1700000000",
                    "type": "system",
                    "system": {"body": "changed number", "type": "customer_changed_number"},
                }],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_mensaje_texto_sin_from_no_invoca_claude(self, client, monkeypatch):
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "Test"}}],
                "messages": [{"id": "wamid.1", "timestamp": "1700000000", "type": "text", "text": {"body": "Hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_mensaje_texto_sin_cuerpo_no_invoca_claude(self, client, monkeypatch):
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "Test"}, "wa_id": "521234"}],
                "messages": [{"from": "521234", "id": "wamid.2", "timestamp": "1700000000", "type": "text", "text": {"body": ""}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_preview_url_sin_body_no_invoca_claude(self, client, monkeypatch):
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "Test"}, "wa_id": "521234"}],
                "messages": [{"from": "521234", "id": "wamid.3", "timestamp": "1700000000", "type": "text", "text": {"preview_url": True}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_texto_sin_remitente_no_invoca_claude(self, client, monkeypatch):
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "Test"}}],
                "messages": [{"id": "wamid.4", "timestamp": "1700000000", "type": "text", "text": {"body": "Hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_texto_con_remitente_y_cuerpo_invoca_claude(self, client, monkeypatch):
        ask_mock = AsyncMock(return_value="respuesta")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        monkeypatch.setattr("app.main.send_text", AsyncMock(return_value=None))
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "Test"}, "wa_id": "521234"}],
                "messages": [{"from": "521234", "id": "wamid.5", "timestamp": "1700000000", "type": "text", "text": {"body": "Hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_called_once_with("Hola")

    def test_campo_distinto_de_messages_ignorado(self, client, monkeypatch):
        """Cambios con field != 'messages' deben ignorarse y devolver 200."""
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [
                {"field": "account_update", "value": {"messaging_product": "whatsapp", "metadata": {}}}
            ]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_value_sin_mensajes_ni_statuses_devuelve_200(self, client, monkeypatch):
        """Un value vacío (solo metadata) no debe generar error."""
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "entry-1", "changes": [
                {"field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                }}
            ]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    # ── BSUID / sender resolution ─────────────────────────────────────────────

    def test_bsuid_payload_invoca_claude(self, client, monkeypatch):
        """Payload con from_user_id (BSUID) debe invocar Claude y enviar respuesta."""
        ask_mock = AsyncMock(return_value="respuesta bsuid")
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        monkeypatch.setattr("app.main.send_text", send_mock)
        resp = client.post("/webhook", json=BSUID_PAYLOAD)
        assert resp.status_code == 200
        ask_mock.assert_called_once_with("Hola desde BSUID")
        send_mock.assert_called_once_with("bsuid_111222333", "respuesta bsuid")

    def test_bsuid_envia_al_from_user_id_no_al_contact(self, client, monkeypatch):
        """send_text debe usar message.from_user_id, no contact.user_id, cuando ambos están presentes."""
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", AsyncMock(return_value="ok"))
        monkeypatch.setattr("app.main.send_text", send_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "U"}, "user_id": "contact_bsuid_999"}],
                "messages": [{"from_user_id": "msg_bsuid_111", "id": "wamid.x", "timestamp": "1", "type": "text", "text": {"body": "hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        send_mock.assert_called_once_with("msg_bsuid_111", "ok")

    def test_ambos_identificadores_prefiere_phone(self, client, monkeypatch):
        """Cuando from (teléfono) y from_user_id (BSUID) están presentes, se usa from."""
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", AsyncMock(return_value="ok"))
        monkeypatch.setattr("app.main.send_text", send_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "U"}, "wa_id": "5212345", "user_id": "bsuid_777"}],
                "messages": [{
                    "from": "5212345",
                    "from_user_id": "bsuid_777",
                    "id": "wamid.dual",
                    "timestamp": "1",
                    "type": "text",
                    "text": {"body": "dual"},
                }],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        send_mock.assert_called_once_with("5212345", "ok")

    def test_fallback_a_contact_user_id(self, client, monkeypatch):
        """Sin from ni from_user_id, debe usar contact.user_id como remitente."""
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", AsyncMock(return_value="ok"))
        monkeypatch.setattr("app.main.send_text", send_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "U"}, "user_id": "contact_bsuid_555"}],
                "messages": [{"id": "wamid.fb", "timestamp": "1", "type": "text", "text": {"body": "hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        send_mock.assert_called_once_with("contact_bsuid_555", "ok")

    def test_sin_ningun_remitente_no_invoca_claude(self, client, monkeypatch):
        """Sin from, from_user_id ni contact.user_id, el mensaje debe descartarse."""
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "p1"},
                "contacts": [{"profile": {"name": "U"}}],
                "messages": [{"id": "wamid.none", "timestamp": "1", "type": "text", "text": {"body": "hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_legacy_envia_al_numero_de_telefono(self, client, monkeypatch):
        """Payload legacy debe enviar la respuesta al número de teléfono del campo from."""
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", AsyncMock(return_value="ok"))
        monkeypatch.setattr("app.main.send_text", send_mock)
        resp = client.post("/webhook", json=TEXT_PAYLOAD)
        assert resp.status_code == 200
        send_mock.assert_called_once_with("521234567890", "ok")

    # ── Regresión: contacts[].profile opcional ────────────────────────────────

    def test_regresion_contacto_sin_profile_devuelve_200(self, client, monkeypatch):
        """Regresión: contact sin profile no debe generar 422."""
        ask_mock = AsyncMock(return_value="ok")
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        monkeypatch.setattr("app.main.send_text", send_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "p1"},
                "contacts": [{"wa_id": "521234567890"}],
                "messages": [{"from": "521234567890", "id": "wamid.r1", "timestamp": "1", "type": "text", "text": {"body": "hola"}}],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_called_once_with("hola")
        send_mock.assert_called_once_with("521234567890", "ok")

    def test_regresion_status_read_devuelve_200(self, client, monkeypatch):
        """Regresión: status update 'read' debe devolver 200 sin invocar Claude."""
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "statuses": [
                    {"id": "wamid.s1", "status": "read", "timestamp": "1700000002", "recipient_id": "521234567890"}
                ],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_regresion_sin_contacts_devuelve_200(self, client, monkeypatch):
        """Regresión: evento sin contacts (p.ej. solo statuses) debe devolver 200."""
        ask_mock = AsyncMock(return_value="x")
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "e1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1234", "phone_number_id": "p1"},
                "statuses": [
                    {"id": "wamid.s2", "status": "delivered", "timestamp": "1700000003", "recipient_id": "521234567890"}
                ],
            }}]}],
        }
        resp = client.post("/webhook", json=payload)
        assert resp.status_code == 200
        ask_mock.assert_not_called()

    def test_respuesta_vacia_de_claude_no_envia_mensaje(self, client, monkeypatch):
        """Si Claude devuelve cadena vacía, no debe llamarse send_text."""
        ask_mock = AsyncMock(return_value="")
        send_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("app.main.ask_claude", ask_mock)
        monkeypatch.setattr("app.main.send_text", send_mock)
        resp = client.post("/webhook", json=TEXT_PAYLOAD)
        assert resp.status_code == 200
        ask_mock.assert_called_once_with("Hola mundo")
        send_mock.assert_not_called()
