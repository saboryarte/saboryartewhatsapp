import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from .claude import ask_claude
from .config import settings
from .whatsapp import WhatsAppPayload, send_text

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


async def _process_payload(payload: WhatsAppPayload) -> None:
    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue

            value = change.value

            for message in value.messages or []:
                if message.type == "text" and message.text:
                    # Sender resolution: phone (legacy) → from_user_id (BSUID) → contact.user_id
                    sender = message.from_ or message.from_user_id
                    if not sender and value.contacts:
                        sender = next((c.user_id for c in value.contacts if c.user_id), None)
                    if not sender:
                        logger.warning("Mensaje de texto ignorado — remitente ausente (id: %s)", message.id)
                        continue
                    body = message.text.get("body", "")
                    if not body:
                        logger.debug("Evento de texto sin cuerpo ignorado (id: %s, keys: %s)", message.id, list(message.text.keys()))
                        continue
                    logger.info("Mensaje de texto recibido (id: %s)", message.id)
                    reply = await ask_claude(body)
                    if reply:
                        await send_text(sender, reply)
                    else:
                        logger.warning("Respuesta de Claude vacía, no se envía mensaje (id: %s)", message.id)
                else:
                    logger.info("Mensaje tipo '%s' ignorado (id: %s)", message.type, message.id)

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
