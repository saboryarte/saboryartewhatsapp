import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from .bold import valid_signature
from .config import settings
from .router import check_unanswered_handoffs, handle_bold_event, handle_choice, handle_team_message, handle_text
from .state import load_conversation, mark_processed, save_conversation
from .usage import remitente_actual
from .whatsapp import WhatsAppContact, WhatsAppMessage, WhatsAppPayload

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

# Cada cuánto se revisan los traspasos que nadie del equipo ha respondido.
_ALERT_CHECK_SECONDS = 60


async def _alert_loop() -> None:
    while True:
        await asyncio.sleep(_ALERT_CHECK_SECONDS)
        try:
            await check_unanswered_handoffs()
        except Exception:
            logger.exception("Error revisando traspasos sin respuesta")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_alert_loop())
    yield
    task.cancel()


app = FastAPI(title="WhatsApp Claude Agent", version="1.0.0", lifespan=lifespan)


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
            value = change.value

            if change.field == "smb_message_echoes":
                await _process_team_echoes(value.message_echoes or [])
                continue
            if change.field != "messages":
                continue

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
                conversation = load_conversation(sender)
                conversation.nombre = _profile_name(value.contacts, sender) or conversation.nombre
                # Las llamadas a Claude de este mensaje quedan registradas a nombre de este cliente.
                token = remitente_actual.set(sender)
                try:
                    if message.type == "text":
                        logger.info("Mensaje de texto recibido (id: %s)", message.id)
                        await handle_text(body, sender, conversation.nombre, conversation)
                    else:
                        logger.info("Respuesta interactiva recibida (id: %s)", message.id)
                        await handle_choice(message.interactive, sender, conversation)
                finally:
                    remitente_actual.reset(token)
                save_conversation(conversation)

            for status_update in value.statuses or []:
                logger.info(
                    "Estado del mensaje %s: %s",
                    status_update.get("id"),
                    status_update.get("status"),
                )


async def _process_team_echoes(echoes: list[WhatsAppMessage]) -> None:
    """Mensajes que el equipo envió desde la app de WhatsApp Business (coexistencia)."""
    for echo in echoes:
        if not mark_processed(echo.id):
            continue
        if not echo.to:
            logger.warning("Eco sin destinatario ignorado (id: %s)", echo.id)
            continue
        logger.info("Mensaje del equipo desde la app (id: %s, tipo: %s)", echo.id, echo.type)
        conversation = load_conversation(echo.to)
        text = (echo.text or {}).get("body", "") if echo.type == "text" else ""
        await handle_team_message(text, echo.to, conversation)
        save_conversation(conversation)


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(payload: WhatsAppPayload, background_tasks: BackgroundTasks) -> dict:
    if payload.object != "whatsapp_business_account":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Objeto no reconocido")
    background_tasks.add_task(_process_payload, payload)
    return {"status": "ok"}


@app.post("/webhooks/bold", status_code=status.HTTP_200_OK)
async def receive_bold_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Avisos de pago de Bold. Bold espera un 200 en menos de 2 s: se verifica la firma y se procesa en segundo plano."""
    body = await request.body()
    if not valid_signature(body, request.headers.get("x-bold-signature")):
        logger.warning("Bold: aviso con firma inválida rechazado")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Firma inválida")
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON inválido")
    background_tasks.add_task(handle_bold_event, event)
    return {"status": "ok"}
