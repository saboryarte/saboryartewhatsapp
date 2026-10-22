import logging
from typing import Any

import httpx
from httpx import HTTPStatusError
from pydantic import BaseModel, ConfigDict, Field

from .config import settings

logger = logging.getLogger(__name__)

_GRAPH_API_URL = "https://graph.facebook.com/v19.0"


# ── Pydantic models ───────────────────────────────────────────────────────────

class WhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from")
    from_user_id: str | None = None
    id: str
    timestamp: str
    type: str
    text: dict[str, Any] | None = None
    image: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    video: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    interactive: dict[str, Any] | None = None


class WhatsAppProfile(BaseModel):
    name: str | None = None


class WhatsAppContact(BaseModel):
    profile: WhatsAppProfile | None = None
    wa_id: str | None = None
    user_id: str | None = None


class WhatsAppValue(BaseModel):
    messaging_product: str = ""
    metadata: dict[str, Any] = {}
    contacts: list[WhatsAppContact] | None = None
    messages: list[WhatsAppMessage] | None = None
    statuses: list[dict[str, Any]] | None = None
    errors: list[dict[str, Any]] | None = None


class WhatsAppChange(BaseModel):
    value: WhatsAppValue
    field: str


class WhatsAppEntry(BaseModel):
    id: str
    changes: list[WhatsAppChange]


class WhatsAppPayload(BaseModel):
    object: str
    entry: list[WhatsAppEntry]


# ── Sending ───────────────────────────────────────────────────────────────────

async def send_text(to: str, text: str) -> bool:
    return await _send(to, {"type": "text", "text": {"body": text}})


async def send_list(to: str, body: str, button: str, rows: list[dict[str, str]], section_title: str = "Opciones") -> bool:
    """Envía un mensaje de lista. Cada fila lleva id, title y, opcionalmente, description."""
    return await _send(to, {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button, "sections": [{"title": section_title, "rows": rows}]},
        },
    })


def interactive_reply(interactive: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extrae {id, title} de la respuesta a una lista o a un botón; None si no es una respuesta."""
    if not interactive:
        return None
    reply_type = interactive.get("type")
    if reply_type not in ("list_reply", "button_reply"):
        return None
    reply = interactive.get(reply_type) or {}
    return reply if reply.get("id") else None


async def _send(to: str, message: dict[str, Any]) -> bool:
    """Envía un mensaje por Graph API. Devuelve True si Meta lo aceptó."""
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        logger.warning("send: WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID no configurados")
        return False

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{_GRAPH_API_URL}/{settings.whatsapp_phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {settings.whatsapp_token}",
                "Content-Type": "application/json",
            },
            json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message},
        )
        try:
            response.raise_for_status()
            logger.info("Mensaje enviado (tipo: %s)", message["type"])
            return True
        except HTTPStatusError as e:
            # Meta explica el motivo en error.message (p. ej. token vencido: code 190, subcode 463)
            try:
                error = e.response.json().get("error", {})
            except ValueError:
                error = {}
            logger.error(
                "Error enviando mensaje: HTTP %s — %s (code %s, subcode %s)",
                e.response.status_code,
                error.get("message", "sin detalle"),
                error.get("code"),
                error.get("error_subcode"),
            )
            return False
