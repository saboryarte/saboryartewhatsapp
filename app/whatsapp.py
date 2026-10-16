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

async def send_text(to: str, text: str) -> None:
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        logger.warning("send_text: WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID no configurados")
        return

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{_GRAPH_API_URL}/{settings.whatsapp_phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {settings.whatsapp_token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
        try:
            response.raise_for_status()
            logger.info("Mensaje enviado")
        except HTTPStatusError as e:
            logger.error("Error enviando mensaje: HTTP %s", e.response.status_code)
