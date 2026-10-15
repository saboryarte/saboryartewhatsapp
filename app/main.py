import logging

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from .claude import ask_claude
from .config import settings
from .whatsapp import WhatsAppPayload, send_text

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Claude Agent", version="1.0.0")


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> str:
    """Verificación del webhook requerida por Meta al configurarlo."""
    if hub_mode != "subscribe":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hub.mode inválido")

    if hub_verify_token != settings.verify_token:
        logger.warning("Intento de verificación con token incorrecto")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token de verificación incorrecto")

    logger.info("Webhook verificado exitosamente")
    return hub_challenge


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(request: Request, payload: WhatsAppPayload) -> dict:
    """Recibe mensajes de WhatsApp, consulta a Claude y responde al remitente."""
    if payload.object != "whatsapp_business_account":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Objeto no reconocido")

    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue

            value = change.value

            for message in value.messages or []:
                sender = message.from_

                if message.type == "text" and message.text:
                    body = message.text.get("body", "")
                    logger.info("Mensaje de %s: %s", sender, body)
                    reply = await ask_claude(body)
                    await send_text(sender, reply)
                else:
                    logger.info("Mensaje tipo '%s' de %s (ignorado)", message.type, sender)

            for status_update in value.statuses or []:
                logger.info(
                    "Estado del mensaje %s: %s",
                    status_update.get("id"),
                    status_update.get("status"),
                )

    return {"status": "ok"}
