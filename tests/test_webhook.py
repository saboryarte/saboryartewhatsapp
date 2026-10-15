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

    def test_parametros_faltantes_devuelven_422(self, client):
        resp = client.get("/webhook")
        assert resp.status_code == 422


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


class TestReceiveWebhook:
    def test_mensaje_texto_devuelve_200(self, client):
        resp = client.post("/webhook", json=TEXT_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_objeto_invalido_devuelve_400(self, client):
        bad = {**TEXT_PAYLOAD, "object": "page"}
        resp = client.post("/webhook", json=bad)
        assert resp.status_code == 400

    def test_actualizacion_estado_devuelve_200(self, client):
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

    def test_cuerpo_invalido_devuelve_422(self, client):
        resp = client.post("/webhook", json={"object": "whatsapp_business_account"})
        assert resp.status_code == 422
