import json
import logging
from typing import Any

import httpx
from httpx import HTTPStatusError
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .archivo import ESPECIA, outgoing_text, record
from .usage import record_whatsapp

logger = logging.getLogger(__name__)

_GRAPH_API_URL = "https://graph.facebook.com/v19.0"


# ── Pydantic models ───────────────────────────────────────────────────────────

class WhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from")
    from_user_id: str | None = None
    # Solo en ecos de coexistencia: destinatario del mensaje que el equipo envió desde la app.
    to: str | None = None
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
    # Ecos de coexistencia (field "smb_message_echoes"): mensajes que el equipo envió desde la app.
    message_echoes: list[WhatsAppMessage] | None = None
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


async def send_buttons(to: str, body: str, buttons: list[dict[str, str]]) -> bool:
    """Envía hasta 3 botones de respuesta. Cada botón lleva id y title."""
    return await _send(to, {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": b} for b in buttons]},
        },
    })


async def send_flow(to: str, body: str, cta: str, flow_id: str, flow_token: str, screen: str, data: dict, mode: str) -> bool:
    """Envía un WhatsApp Flow (formulario dentro del chat) que abre en «screen» con los datos iniciales «data»."""
    return await _send(to, {
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": body},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": cta,
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": screen, "data": data},
                    "mode": mode,
                },
            },
        },
    })


def flow_response(interactive: dict[str, Any] | None) -> dict[str, Any] | None:
    """Datos que devolvió un Flow al completarse (interactive «nfm_reply»), o None si no es una respuesta de Flow."""
    if not interactive or interactive.get("type") != "nfm_reply":
        return None
    try:
        return json.loads((interactive.get("nfm_reply") or {}).get("response_json") or "")
    except ValueError:
        return None


def interactive_reply(interactive: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extrae {id, title} de la respuesta a una lista o a un botón; None si no es una respuesta."""
    if not interactive:
        return None
    reply_type = interactive.get("type")
    if reply_type not in ("list_reply", "button_reply"):
        return None
    reply = interactive.get(reply_type) or {}
    return reply if reply.get("id") else None


# Límite para comprobantes: una foto o un PDF de un banco pesa mucho menos.
MAX_MEDIA_BYTES = 10 * 1024 * 1024


async def download_media(media_id: str) -> tuple[bytes, str] | None:
    """Descarga una foto o un documento que envió el cliente. Devuelve (contenido, tipo MIME) o None si falla.

    Meta entrega primero una URL temporal (vence en minutos) y luego el archivo, ambos con el token del bot.
    """
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            info = await client.get(f"{_GRAPH_API_URL}/{media_id}", headers=headers)
            info.raise_for_status()
            data = info.json()
            if int(data.get("file_size") or 0) > MAX_MEDIA_BYTES:
                logger.warning("Archivo del cliente demasiado grande (%s bytes)", data.get("file_size"))
                return None
            response = await client.get(data["url"], headers=headers)
            response.raise_for_status()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error("No se pudo descargar el archivo del cliente (%s)", type(e).__name__)
        return None
    if len(response.content) > MAX_MEDIA_BYTES:
        return None
    return response.content, (data.get("mime_type") or "").split(";")[0].strip()


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
            record_whatsapp(to, message["type"])
            record(to, ESPECIA, outgoing_text(message))
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
