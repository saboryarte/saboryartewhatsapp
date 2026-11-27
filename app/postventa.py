"""Postventa (O4): «¿Cómo te llegó tu pedido?», 3 días después de la entrega, solo a quien aceptó en P15.

Pasadas 24 h desde el último mensaje del cliente, WhatsApp solo deja escribirle con una plantilla aprobada por Meta,
así que O4 es la plantilla «especia_postventa». Sus botones traen el id de la opción (o4_bien / o4_problema) y la
conversación sigue en app/router.py (O4B, O4C, O4M, O4S, O4F o la queja Q1).

En el servidor (cron, todos los días a las 10:00 a. m., en horario de atención por si responde «Tuve un problema»):
    .venv/bin/python -m app.postventa diario
Otros comandos:
    .venv/bin/python -m app.postventa pendientes          # a quién le toca y cuándo, sin enviar nada
    .venv/bin/python -m app.postventa enviar SAB-00001    # enviarla ya a un pedido pagado (para probar)
    .venv/bin/python -m app.postventa crear-plantilla     # pedirle a Meta que apruebe la plantilla
    .venv/bin/python -m app.postventa estado-plantilla    # ver si Meta ya la aprobó

La plantilla pertenece a la cuenta de WhatsApp (WABA) de la llave: al pasar al número del negocio hay que crearla
otra vez allá con «crear-plantilla».
"""
import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

from . import conexion
from .config import conexion_path, settings
from .flows import button_rows, get_step
from .handoff import COLOMBIA, dispatch_day
from .orders import shipping_zone
from .pedidos import PAGADO, Pedido, follow_ups_pending, get_order, update_order
from .state import load_conversation, save_conversation
from .whatsapp import send_template

logger = logging.getLogger(__name__)

PLANTILLA = "especia_postventa"
IDIOMA = "es"
# Días hábiles de entrega después del despacho (los mismos de P7B) y días que se esperan después de la entrega.
_DIAS_DE_ENTREGA = {"cerca": 2, "resto": 4}
_DIAS_DESPUES_DE_LA_ENTREGA = 3
# Si el cron no corrió unos días, se pone al día; más atrás ya no se pregunta.
_DIAS_DE_GRACIA = 7


def _add_business_days(start: date, days: int) -> date:
    current = start
    while days:
        current += timedelta(days=1)
        if current.weekday() < 5:  # lunes a viernes
            days -= 1
    return current


def due_date(order: Pedido) -> date:
    """Día en que le toca la postventa: despacho + días hábiles de entrega + 3 días."""
    paid = datetime.fromtimestamp(order.pagado_en or order.creado_en, tz=COLOMBIA)
    zona = "resto" if shipping_zone(order.envio.get("ciudad", "")) == "resto_del_pais" else "cerca"
    delivered = _add_business_days(dispatch_day(paid), _DIAS_DE_ENTREGA[zona])
    return delivered + timedelta(days=_DIAS_DESPUES_DE_LA_ENTREGA)


def _first_name(order: Pedido) -> str:
    name = (order.nombre or order.envio.get("nombre") or "").strip().split()
    first = name[0] if name else ""
    # Meta no acepta un parámetro vacío: sin nombre, «¡Hola, de nuevo!».
    return first.capitalize() if first.isalpha() else "de nuevo"


def message_text(order: Pedido) -> str:
    """El mensaje tal como lo ve el cliente (O4 con sus datos)."""
    return get_step("O4")["texto"].replace("[nombre]", _first_name(order)).replace("[referencia]", order.referencia)


def _components(order: Pedido) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = [{"type": "body", "parameters": [
        {"type": "text", "text": _first_name(order)}, {"type": "text", "text": order.referencia}]}]
    # El payload de cada botón es el id de la opción de O4: al tocarlo llega como si fuera un botón interactivo.
    for index, button in enumerate(button_rows("O4")):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(index),
                           "parameters": [{"type": "payload", "payload": button["id"]}]})
    return components


async def send_follow_up(order: Pedido) -> bool:
    text = message_text(order)
    if not await send_template(order.remitente, PLANTILLA, IDIOMA, _components(order), readable=text):
        return False
    order.postventa_enviada_en = time.time()
    update_order(order)
    # Si responde escribiendo («llegó perfecto») en vez de tocar el botón, también cuenta.
    conversation = load_conversation(order.remitente)
    conversation.opciones = {"paso": "O4", "tipo": "botones", "cuerpo": text, "boton": None, "filas": button_rows("O4")}
    save_conversation(conversation)
    logger.info("Postventa enviada para el pedido %s", order.referencia)
    return True


def due_follow_ups(today: date) -> list[Pedido]:
    return [o for o in follow_ups_pending() if today - timedelta(days=_DIAS_DE_GRACIA) <= due_date(o) <= today]


async def run_daily(today: date) -> int:
    sent = 0
    for order in due_follow_ups(today):
        if load_conversation(order.remitente).en_traspaso:
            logger.info("Postventa de %s aplazada: Juanita está atendiendo al cliente", order.referencia)
            continue
        if await send_follow_up(order):
            sent += 1
        else:
            logger.error("Postventa de %s no enviada (¿la plantilla ya está aprobada?)", order.referencia)
    logger.info("Postventa del %s: %d enviada(s)", today.isoformat(), sent)
    return sent


# ── Plantilla en Meta ─────────────────────────────────────────────────────────

def template_definition() -> dict[str, Any]:
    step = get_step("O4")
    body = step["texto"].replace("[nombre]", "{{1}}").replace("[referencia]", "{{2}}")
    return {
        "name": PLANTILLA, "language": IDIOMA, "category": "UTILITY",
        "components": [
            {"type": "BODY", "text": body, "example": {"body_text": [["Laura", "SAB-00012"]]}},
            {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": b["title"]} for b in button_rows("O4")]},
        ],
    }


async def _waba_id() -> str:
    """Cuenta de WhatsApp a la que pertenece la plantilla: la del número del negocio si ya se conectó, si no
    WHATSAPP_WABA_ID del .env, y si no la que diga la llave."""
    try:
        datos = json.loads(conexion_path().read_text())
        if datos.get("waba_id"):
            return datos["waba_id"]
    except (OSError, ValueError):
        pass
    return settings.whatsapp_waba_id or await conexion._waba_from_token(settings.whatsapp_token)


async def create_template() -> dict[str, Any]:
    waba = await _waba_id()
    return await conexion._graph("POST", f"{waba}/message_templates", settings.whatsapp_token, json=template_definition())


async def template_status() -> list[dict[str, Any]]:
    waba = await _waba_id()
    data = await conexion._graph("GET", f"{waba}/message_templates", settings.whatsapp_token, params={
        "name": PLANTILLA, "fields": "name,language,status,category,rejected_reason"})
    return data.get("data", [])


def _find(reference: str) -> Pedido | None:
    match = re.fullmatch(r"SAB-(\d+)", reference.strip().upper())
    return get_order(int(match.group(1))) if match else None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.postventa")
    sub = parser.add_subparsers(dest="comando", required=True)
    diario = sub.add_parser("diario")
    diario.add_argument("--fecha", type=date.fromisoformat)
    sub.add_parser("pendientes")
    enviar = sub.add_parser("enviar")
    enviar.add_argument("referencia")
    sub.add_parser("crear-plantilla")
    sub.add_parser("estado-plantilla")
    args = parser.parse_args()

    today = datetime.now(COLOMBIA).date()
    if args.comando == "diario":
        asyncio.run(run_daily(args.fecha or today))
    elif args.comando == "pendientes":
        pending = follow_ups_pending()
        for order in pending:
            print(f"{order.referencia}  {order.nombre or ''}  le toca el {due_date(order).isoformat()}")
        print(f"{len(pending)} pedido(s) esperan la postventa.")
    elif args.comando == "enviar":
        order = _find(args.referencia)
        if order is None or order.estado != PAGADO:
            print(f"{args.referencia}: no hay un pedido pagado con esa referencia.")
            return 1
        ok = asyncio.run(send_follow_up(order))
        print("Enviada." if ok else "No se pudo enviar (¿la plantilla ya está aprobada?).")
        return 0 if ok else 1
    elif args.comando == "crear-plantilla":
        print(asyncio.run(create_template()))
    else:
        for template in asyncio.run(template_status()) or [{"status": "no existe"}]:
            print(template)
    return 0


if __name__ == "__main__":
    sys.exit(main())
