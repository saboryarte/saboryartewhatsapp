import asyncio
import hashlib
import hmac
import html
import json
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError

from . import archivo, conexion, precios, transferencias
from .bold import valid_signature
from .config import settings
from .notify import notify_team
from .precios import current_prices
from .flows import format_price
from .orders import describe_item
from .pedidos import get_order
from .router import (
    check_unanswered_handoffs, handle_bold_event, handle_choice, handle_media, handle_team_message, handle_text,
    resolve_transfer,
)
from .state import load_conversation, mark_processed, save_conversation
from .usage import remitente_actual
from .whatsapp import WhatsAppContact, WhatsAppMessage, WhatsAppPayload, flow_response, interactive_reply

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

# Cada cuánto se revisan los traspasos que nadie del equipo ha respondido.
_ALERT_CHECK_SECONDS = 60
# Cada cuánto se borra lo que ya cumplió su plazo de retención (archivo.py).
_PURGE_SECONDS = 24 * 3600


def _purge() -> None:
    try:
        archivo.purge()
    except Exception:
        logger.exception("Error aplicando la retención del archivo de chats")


async def _alert_loop() -> None:
    last_purge = 0.0
    while True:
        await asyncio.sleep(_ALERT_CHECK_SECONDS)
        try:
            await check_unanswered_handoffs()
        except Exception:
            logger.exception("Error revisando traspasos sin respuesta")
        if time.time() - last_purge >= _PURGE_SECONDS:
            _purge()
            last_purge = time.time()


# Fotos y documentos: se aceptan como comprobante de una transferencia.
_MEDIA_TYPES = ("image", "document")
# Avisos de Meta sobre la conexión del número en coexistencia (app/conexion.py).
_CONNECTION_FIELDS = ("account_update", "history", "smb_app_state_sync")


def _incoming_text(message: WhatsAppMessage) -> str:
    """Texto legible de lo que envió el cliente, para el archivo de chats."""
    if message.type == "text":
        return (message.text or {}).get("body", "")
    response = flow_response(message.interactive)
    if response is not None:
        return f"[Formulario] {json.dumps(response, ensure_ascii=False)}"
    if message.type in _MEDIA_TYPES:
        media = getattr(message, message.type) or {}
        name = media.get("filename") or media.get("caption") or ""
        return f"[{'Foto' if message.type == 'image' else 'Documento'}{': ' + name if name else ''}]"
    reply = interactive_reply(message.interactive)
    return f"[Tocó] {reply.get('title', reply['id'])}" if reply else "[Mensaje interactivo]"


async def _check_prices() -> None:
    """Carga los precios al arrancar: si precios.yaml tiene un error, se avisa al equipo de inmediato."""
    current_prices()
    if precios.load_error:
        await notify_team(
            "[Saboryarte] ⚠️ Error en precios.yaml",
            f"El archivo contenido/precios.yaml tiene un error: {precios.load_error}.\n\n"
            "Especia sigue cobrando con los últimos precios correctos. Corrige el archivo en GitHub y avísale "
            "a quien administra el bot para que lo reinicie.",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _check_prices()
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
            if change.field in _CONNECTION_FIELDS:
                await conexion.handle_event(change.field, value.model_dump())
                continue
            if change.field != "messages":
                continue

            for message in value.messages or []:
                if not mark_processed(message.id):
                    logger.info("Mensaje duplicado ignorado (id: %s)", message.id)
                    continue
                if message.type not in ("text", "interactive", *_MEDIA_TYPES):
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
                archivo.record(sender, archivo.CLIENTE, _incoming_text(message))
                archivo.touch_client(sender)
                # Las llamadas a Claude de este mensaje quedan registradas a nombre de este cliente.
                token = remitente_actual.set(sender)
                try:
                    if message.type == "text":
                        logger.info("Mensaje de texto recibido (id: %s)", message.id)
                        await handle_text(body, sender, conversation.nombre, conversation)
                    elif message.type in _MEDIA_TYPES:
                        logger.info("Archivo recibido (id: %s, tipo: %s)", message.id, message.type)
                        await handle_media(message.type, getattr(message, message.type) or {}, sender, conversation)
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
        archivo.record(echo.to, archivo.EQUIPO, text or f"[{echo.type}]")
        await handle_team_message(text, echo.to, conversation)
        save_conversation(conversation)


def _valid_meta_signature(body: bytes, header: str | None) -> bool:
    """Meta firma cada aviso con la llave secreta de la app (X-Hub-Signature-256: sha256=<hex>)."""
    if not settings.app_secret:
        return True  # sin APP_SECRET (pruebas locales) no se puede verificar
    expected = "sha256=" + hmac.new(settings.app_secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(expected, header)


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    body = await request.body()
    if not _valid_meta_signature(body, request.headers.get("x-hub-signature-256")):
        # Sin esta verificación cualquiera podría hacerse pasar por un cliente o por el equipo (y silenciar al bot).
        logger.warning("Aviso de WhatsApp con firma inválida rechazado")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Firma inválida")
    try:
        payload = WhatsAppPayload.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
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


# ── Transferencias: botones «Confirmar pago» / «No llegó» del correo del comprobante ──

def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status_code, content=(
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Saboryarte</title>'
        '<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;color:#222}'
        'button{font:600 17px system-ui;padding:.8rem 1.4rem;border:0;border-radius:8px;color:#fff;cursor:pointer}'
        '.ok{background:#5B7F3A}.no{background:#9A3B2E}pre{white-space:pre-wrap;background:#f5f2ea;padding:1rem;'
        f'border-radius:8px}}</style></head><body><h1 style="font-size:1.3rem">{html.escape(title)}</h1>{body}</body></html>'
    ))


def _checked_order(order_id: int, accion: str, token: str):
    order = get_order(order_id)
    if order is None or not transferencias.valid_token(order_id, accion, token):
        return None
    return order


@app.get("/transferencias/{order_id}/{accion}", response_class=HTMLResponse)
async def transfer_page(order_id: int, accion: str, t: str = "") -> HTMLResponse:
    """Primer toque desde el correo: muestra el pedido y pide confirmar (abrir el enlace no cambia nada)."""
    order = _checked_order(order_id, accion, t)
    if order is None:
        return _page("Enlace no válido", "<p>Este enlace no es válido. Usa el botón del correo del comprobante.</p>", 403)
    resumen = "\n".join([*(describe_item(i) for i in order.items), f"Total: {format_price(order.total)}",
                         f"Cliente: {order.nombre or 'sin nombre'} · WhatsApp {order.remitente}"])
    confirmar = accion == transferencias.CONFIRMAR
    pregunta = ("¿Ya verificaste en Bancolombia que llegó la transferencia por " + format_price(order.total) + "?"
                if confirmar else "¿Le pedimos al cliente que revise y envíe el comprobante otra vez?")
    boton = "Sí, confirmar el pago" if confirmar else "Sí, avisarle que no llegó"
    return _page(f"Pedido {order.referencia}", (
        f"<pre>{html.escape(resumen)}</pre><p>{html.escape(pregunta)}</p>"
        f'<form method="post"><input type="hidden" name="t" value="{html.escape(t)}">'
        f'<button class="{"ok" if confirmar else "no"}">{boton}</button></form>'
    ))


@app.post("/transferencias/{order_id}/{accion}", response_class=HTMLResponse)
async def transfer_action(order_id: int, accion: str, request: Request) -> HTMLResponse:
    # Formulario simple (application/x-www-form-urlencoded): se lee a mano para no depender de python-multipart.
    t = (parse_qs((await request.body()).decode("utf-8", "replace")).get("t") or [""])[0]
    order = _checked_order(order_id, accion, t)
    if order is None:
        return _page("Enlace no válido", "<p>Este enlace no es válido. Usa el botón del correo del comprobante.</p>", 403)
    logger.info("Transferencia del pedido %s: el equipo eligió «%s»", order.referencia, accion)
    result = await resolve_transfer(order, accion)
    return _page(f"Pedido {order.referencia}", f"<p>{html.escape(result)}</p>")


# ── Conexión del número del negocio (coexistencia) ──

@app.get("/conectar", response_class=HTMLResponse)
async def signup_page(exp: int = 0, t: str = "") -> HTMLResponse:
    if not conexion.valid_link(exp, t):
        return _page("Enlace no válido", "<p>Este enlace no es válido o ya venció. Pide uno nuevo.</p>", 403)
    return _page("Conectar el WhatsApp de Saboryarte", conexion.signup_page(exp, t))


@app.post("/conectar")
async def signup_finish(request: Request) -> JSONResponse:
    try:
        datos = json.loads(await request.body())
        exp, t, code = int(datos.get("exp", 0)), str(datos.get("t", "")), str(datos.get("code", ""))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False, "mensaje": "Solicitud inválida."}, status_code=400)
    if not conexion.valid_link(exp, t):
        return JSONResponse({"ok": False, "mensaje": "El enlace venció o no es válido. Pide uno nuevo."}, status_code=403)
    if not code:
        return JSONResponse({"ok": False, "mensaje": "Meta no entregó el código de conexión. Intenta de nuevo."}, status_code=400)
    try:
        mensaje = await conexion.connect(code, str(datos.get("waba_id") or ""), str(datos.get("phone_number_id") or ""))
    except conexion.ConexionError as e:
        logger.error("Conexión del número fallida: %s", e)
        return JSONResponse({"ok": False, "mensaje": f"No se pudo conectar: {e}"}, status_code=502)
    except Exception:
        logger.exception("Error inesperado conectando el número")
        return JSONResponse({"ok": False, "mensaje": "No se pudo conectar por un error del servidor. Intenta de nuevo."},
                            status_code=500)
    return JSONResponse({"ok": True, "mensaje": mensaje})
