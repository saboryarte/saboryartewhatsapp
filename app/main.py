import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import settings
from .router import handle_choice, handle_text
from .state import load_conversation, mark_processed, save_conversation
from .whatsapp import WhatsAppContact, WhatsAppMessage, WhatsAppPayload

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Claude Agent", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]
    logger.warning("%s %s 422 — campo(s) inválidos: %s", request.method, request.url.path, safe_errors)
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    """Verificación del webhook requerida por Meta al configurarlo."""
    if hub_mode is None or hub_verify_token is None or hub_challenge is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Parámetros de verificación incompletos")

    if hub_mode != "subscribe":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hub.mode inválido")

    if hub_verify_token != settings.verify_token:
        logger.warning("Intento de verificación con token incorrecto")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token de verificación incorrecto")

    logger.info("Webhook verificado exitosamente")
    return hub_challenge


def _profile_name(contacts: list[WhatsAppContact] | None, sender: str) -> str | None:
    """Nombre de perfil del remitente; si hay un único contacto se asume que es él."""
    if not contacts:
        return None
    contact = next((c for c in contacts if sender in (c.wa_id, c.user_id)), None)
    if contact is None and len(contacts) == 1:
        contact = contacts[0]
    return contact.profile.name if contact and contact.profile else None


def _resolve_sender(message: WhatsAppMessage, contacts: list[WhatsAppContact] | None) -> str | None:
    # Sender resolution: phone (legacy) → from_user_id (BSUID) → contact.user_id
    sender = message.from_ or message.from_user_id
    if not sender and contacts:
        sender = next((c.user_id for c in contacts if c.user_id), None)
    return sender


async def _process_payload(payload: WhatsAppPayload) -> None:
    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue

            value = change.value

            for message in value.messages or []:
                if not mark_processed(message.id):
                    logger.info("Mensaje duplicado ignorado (id: %s)", message.id)
                    continue
                if message.type not in ("text", "interactive"):
                    logger.info("Mensaje tipo '%s' ignorado (id: %s)", message.type, message.id)
                    continue
                sender = _resolve_sender(message, value.contacts)
                if not sender:
                    logger.warning("Mensaje ignorado — remitente ausente (id: %s)", message.id)
                    continue
                if message.type == "text":
                    body = (message.text or {}).get("body", "")
                    if not body:
                        logger.debug("Evento de texto sin cuerpo ignorado (id: %s, keys: %s)", message.id, list((message.text or {}).keys()))
                        continue
                    logger.info("Mensaje de texto recibido (id: %s)", message.id)
                    conversation = load_conversation(sender)
                    await handle_text(body, sender, _profile_name(value.contacts, sender), conversation)
                else:
                    logger.info("Respuesta interactiva recibida (id: %s)", message.id)
                    conversation = load_conversation(sender)
                    await handle_choice(message.interactive, sender, conversation)
                save_conversation(conversation)

            for status_update in value.statuses or []:
                logger.info(
                    "Estado del mensaje %s: %s",
                    status_update.get("id"),
                    status_update.get("status"),
                )


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(payload: WhatsAppPayload, background_tasks: BackgroundTasks) -> dict:
    if payload.object != "whatsapp_business_account":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Objeto no reconocido")
    background_tasks.add_task(_process_payload, payload)
    return {"status": "ok"}
