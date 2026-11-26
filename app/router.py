"""Decide qué responder a cada mensaje según el estado de la conversación y los pasos de mensajes.json.

Rutas (artefacto de noviembre de 2026): el menú tiene cuatro opciones. «Comprar mis mezclas» la atiende Especia de
principio a fin, incluido el pago; regalos corporativos, distribuidores y proveedores pasan directo a Juanita.
"""
import logging
import re
import time
from datetime import datetime

from .claude import ask_claude, extract_order, extract_shipping, is_serious_complaint
from .flows import (
    MENU_PRINCIPAL,
    button_rows,
    format_price,
    find_any_option,
    flow_of,
    get_product,
    get_step,
    list_rows,
    mix_ids,
    ordered_mix_ids,
    step_text,
)
from .greetings import is_greeting, is_human_request, is_menu_request, normalize_text, pick_welcome
from .handoff import (
    COLOMBIA,
    MOTIVOS,
    business_minutes_between,
    dispatch_day,
    end_handoff,
    handoff_ficha,
    is_closing_message,
    is_open,
    needs_alert,
    register_team_message,
    spanish_date,
    start_handoff,
    team_went_idle,
)
from . import archivo, bold
from .notify import notify_team
from .orders import (
    ask_missing_shipping,
    count_kits,
    describe_item,
    find_mixes,
    missing_shipping_fields,
    order_summary,
    parse_quantity,
    same_city,
    shipping_cost,
    shipping_zone,
    subtotal,
)
from .config import settings
from .prompt import DESTINOS, RUTAS_ESCRITAS
from .pedidos import (
    ANULADO,
    ESPERANDO_COMPROBANTE,
    ESTADOS,
    PAGADO,
    PENDIENTE,
    POR_VERIFICAR,
    REVISAR,
    TRANSFERENCIA,
    Pedido,
    create_order,
    find_by_link,
    find_by_reference,
    get_order,
    last_paid,
    order_ficha,
    pending_transfer,
    update_order,
)
from . import transferencias
from .state import Conversation, list_handoffs, load_conversation, mark_processed, save_conversation
from .whatsapp import download_media, interactive_reply, send_buttons, send_list, send_text

logger = logging.getLogger(__name__)

# Pasos que se muestran tal cual cuando el cliente elige una opción que lleva a ellos.
_PASOS_SIMPLES = frozenset({"G1", "P1", "P4", "P4B"})
# Opciones del menú que pasan directo a Juanita, sin preguntas del bot: destino → motivo del traspaso.
_DIRECTOS = {"R1": "corporativo", "D1": "distribuidor", "V1": "proveedor"}
# Pasos de otras rutas que se usan como respuesta dentro de la ruta actual: no cambian el flujo activo.
_FLUJOS_DE_APOYO = frozenset({"entrada", "otras"})


def _now() -> datetime:
    return datetime.now(COLOMBIA)


async def _show_step(
    sender: str, step_id: str, conversation: Conversation, body: str | None = None, rows: list[dict] | None = None,
) -> str | None:
    """Envía un paso según su tipo y actualiza el estado. Devuelve el texto enviado, o None si Meta lo rechazó."""
    step = get_step(step_id)
    text = body or step_text(step_id)
    shown_rows, list_button = None, None
    if step["tipo"] == "lista":
        shown_rows, list_button = rows or list_rows(step_id), step["boton_lista"]
        sent = await send_list(sender, text, list_button, shown_rows)
    elif step["tipo"] == "botones":
        shown_rows = button_rows(step_id)
        sent = await send_buttons(sender, text, shown_rows)
    else:
        sent = await send_text(sender, text)
    if not sent:
        return None
    conversation.opciones = {"paso": step_id, "tipo": step["tipo"], "cuerpo": text, "boton": list_button, "filas": shown_rows}
    if step_id == MENU_PRINCIPAL:
        conversation.reset_flow()
    elif flow_of(step_id) not in _FLUJOS_DE_APOYO:
        conversation.flujo_activo = flow_of(step_id)
    conversation.paso = step_id
    conversation.add_step(step_id)
    return text


async def _reply_text(sender: str, text: str) -> str | None:
    return text if await send_text(sender, text) else None


def _bot_is_silenced(conversation: Conversation) -> bool:
    """True mientras una persona atiende. Si el equipo ya respondió y lleva tiempo sin escribir, el bot retoma."""
    if not conversation.en_traspaso:
        return False
    if team_went_idle(conversation, time.time()):
        logger.info("El equipo lleva tiempo sin escribir: el bot retoma la conversación")
        end_handoff(conversation)
        return False
    logger.info("Conversación en traspaso: el bot no responde")
    return True


async def handle_text(body: str, sender: str, profile_name: str | None, conversation: Conversation) -> None:
    # «menú» es la salida del cliente: termina el traspaso aunque nadie del equipo haya respondido.
    if conversation.en_traspaso and is_menu_request(body):
        logger.info("El cliente pidió el menú: termina el traspaso")
        end_handoff(conversation)
    if _bot_is_silenced(conversation):
        return
    if is_human_request(body):
        sent = await _handoff(sender, conversation, "solicitud")
    elif is_menu_request(body):
        logger.info("Menú solicitado, se reinicia el flujo")
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation)
    elif option := _typed_option(body, conversation):
        # Escribir el texto de una opción visible cuenta como tocarla.
        logger.info("Texto reconocido como la opción '%s'", option["id"])
        await handle_choice({"type": "list_reply", "list_reply": option}, sender, conversation)
        return
    elif _asks_to_see_options(body, conversation):
        sent = await _resend(sender, conversation, conversation.opciones["cuerpo"])
    elif conversation.flujo_activo is None and is_greeting(body):
        logger.info("Saludo detectado, se envía el menú")
        # El saludo siempre recibe una bienvenida (al azar) con el menú, a cualquier hora: el aviso de horario
        # solo aparece cuando el cliente pide una persona (T2) o cuando se le pasa a Juanita.
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation, body=pick_welcome(profile_name))
    elif is_greeting(body):
        # Un saludo a mitad de un paso no lo reinicia: se retoma donde iba, con sus opciones.
        sent = await _resume(sender, conversation, "¡Hola de nuevo! 🌿 Sigamos donde íbamos.")
    elif conversation.paso == "P4M":
        sent = await _take_kit_mini(body, sender, conversation)
    elif conversation.paso in ("P6", "P4K"):
        sent = await _take_order_items(body, sender, conversation)
    elif conversation.paso == "P7":
        sent = await _take_city(body, sender, conversation)
    elif conversation.paso == "P7C" and _is_yes(body):
        sent = await _use_saved_shipping(sender, conversation)
    elif conversation.paso in ("P7B", "P7C", "P7D"):
        sent = await _take_shipping(body, sender, conversation)
    elif conversation.paso == "Q1":
        sent = await _take_complaint(body, sender, conversation)
    elif conversation.paso == "O3":
        conversation.datos["detalle"] = body
        sent = await _handoff(sender, conversation, "alianza")
    elif conversation.flujo_activo is None and not conversation.historial:
        # Primer mensaje de la conversación y no es un saludo: se responde y se ofrece el menú en el mismo mensaje.
        sent = await _answer_with_menu(body, sender, conversation)
    else:
        sent = await _answer_and_resume(body, sender, conversation)
    if sent:
        conversation.add_turn(body, sent)


# Límite del cuerpo de un mensaje con lista en WhatsApp.
_MAX_CUERPO_LISTA = 1024


async def _answer_with_menu(body: str, sender: str, conversation: Conversation) -> str | None:
    """La conversación siempre empieza con el menú: la respuesta de Claude va como texto de la lista de opciones."""
    reply = await ask_claude(body, conversation.historial)
    if not reply or reply.strip().upper().startswith(_NO_ENTIENDO):
        return await _show_step(sender, MENU_PRINCIPAL, conversation)
    if await _navigate(reply, body, sender, conversation):
        return None
    if _IR.match(reply):
        return await _show_step(sender, MENU_PRINCIPAL, conversation)
    if len(reply) <= _MAX_CUERPO_LISTA:
        return await _show_step(sender, MENU_PRINCIPAL, conversation, body=reply)
    sent = await _reply_text(sender, reply)
    return "\n\n".join(filter(None, [sent, await _show_step(sender, MENU_PRINCIPAL, conversation)]))


# ── Nunca dejar al cliente sin opciones ───────────────────────────────────────

_INTERACTIVOS = ("lista", "botones")
_NO_ENTIENDO = "NO_ENTIENDO"
_IR = re.compile(r"^\s*IR:\s*([a-z0-9_]+)\s*$", re.IGNORECASE)


async def _navigate(reply: str, body: str, sender: str, conversation: Conversation) -> bool:
    """Si Claude respondió «IR:<destino>», lleva al cliente ahí como si hubiera tocado la opción. True si lo hizo
    (la respuesta y el turno ya quedaron guardados)."""
    match = _IR.match(reply)
    target = match.group(1).lower() if match else None
    if target in RUTAS_ESCRITAS:
        logger.info("Texto llevado a la ruta '%s'", target)
        sent = await _written_route(target, sender, conversation)
        if sent:
            conversation.add_turn(body, sent)
        return True
    found = find_any_option(target) if target else None
    if not found or found[1]["id"] not in DESTINOS:
        return False
    option = found[1]
    logger.info("Texto llevado a la opción '%s'", option["id"])
    await handle_choice({"type": "list_reply", "list_reply": {"id": option["id"], "title": option["titulo"]}}, sender, conversation)
    return True


async def _written_route(target: str, sender: str, conversation: Conversation) -> str | None:
    """Rutas a las que se llega escribiendo: queja (Q1), alianza (O3) y «por ahora no» (P10)."""
    if target == "queja":
        return await _show_step(sender, "Q1", conversation)
    if target == "alianza":
        return await _show_step(sender, "O3", conversation)
    sent = await _show_step(sender, "P10", conversation)
    conversation.reset_flow()
    return sent


def _join(first: str, second: str | None, limit: int = _MAX_CUERPO_LISTA) -> str:
    """Une dos textos en un mensaje si caben en el límite; si no, solo el primero."""
    joined = f"{first}\n\n{second}" if second else first
    return joined if len(joined) <= limit else first


def _typed_option(body: str, conversation: Conversation) -> dict | None:
    """La opción visible cuyo texto escribió el cliente («Kit mini», «Link de pago»…), si es una sola."""
    rows = (conversation.opciones or {}).get("filas") or []
    text = normalize_text(body)
    if not text or not rows:
        return None
    titles = [(row, normalize_text(row["title"])) for row in rows]
    exact = [row for row, title in titles if title == text]
    if exact:
        return {"id": exact[0]["id"], "title": exact[0]["title"]}
    if len(text) >= 4:
        partial = [row for row, title in titles if re.search(rf"\b{re.escape(text)}\b", title)]
        if len(partial) == 1:
            return {"id": partial[0]["id"], "title": partial[0]["title"]}
    return None


_SI = {"si", "sii", "dale", "ok", "listo", "claro", "de una", "esa", "esa misma", "la misma", "si claro", "si senor"}


def _is_yes(body: str) -> bool:
    return normalize_text(body) in _SI


def _asks_to_see_options(body: str, conversation: Conversation) -> bool:
    """El cliente escribió el botón que abre la lista («Ver opciones», «Ver kits»…)."""
    shown = conversation.opciones or {}
    if shown.get("tipo") not in _INTERACTIVOS or not shown.get("boton"):
        return False
    return normalize_text(body) == normalize_text(shown["boton"])


async def _resend(sender: str, conversation: Conversation, body: str) -> str | None:
    """Vuelve a enviar las últimas opciones (lista o botones) con otro texto. No cambia el paso."""
    shown = conversation.opciones
    if not shown:
        return await _show_step(sender, MENU_PRINCIPAL, conversation, body=body)
    limit = _MAX_CUERPO_LISTA if shown["tipo"] in _INTERACTIVOS else 4096
    if len(body) > limit:
        # Texto demasiado largo para ir con las opciones: va aparte y luego las opciones con su texto original.
        first = await _reply_text(sender, body)
        return _join(first or "", await _resend(sender, conversation, shown["cuerpo"]), 10_000) or None
    if shown["tipo"] == "lista":
        sent = await send_list(sender, body, shown["boton"], shown["filas"])
    elif shown["tipo"] == "botones":
        sent = await send_buttons(sender, body, shown["filas"])
    else:
        sent = await send_text(sender, body)
    if sent:
        conversation.opciones = {**shown, "cuerpo_enviado": body}
    return body if sent else None


async def _resume(sender: str, conversation: Conversation, intro: str) -> str | None:
    """Retoma el paso en curso: el aviso más la pregunta y las opciones que el cliente tenía a la vista."""
    shown = conversation.opciones
    if not shown:
        return await _show_step(sender, MENU_PRINCIPAL, conversation)
    limit = _MAX_CUERPO_LISTA if shown["tipo"] in _INTERACTIVOS else 4096
    return await _resend(sender, conversation, _join(intro, shown["cuerpo"], limit))


async def _shortcuts(sender: str, conversation: Conversation, reply: str) -> str | None:
    """Respuesta fuera de un paso guiado: llega con «Ver menú» y «Hablar con alguien» para no quedar sin salida.
    Si el bot estaba esperando datos (p. ej. el envío), la pregunta pendiente se repite al final."""
    shown = conversation.opciones or {}
    pending = shown.get("cuerpo") if shown.get("tipo") == "libre" else None
    body = _join(reply, pending)
    if len(body) > _MAX_CUERPO_LISTA:
        sent_reply = await _reply_text(sender, reply)
        body = pending or "¿Te ayudo con algo más? 🌿"
        sent = await send_buttons(sender, body, button_rows("G5"))
        return _join(sent_reply or "", body if sent else None, 10_000) or None
    if not await send_buttons(sender, body, button_rows("G5")):
        return None
    if not pending:
        # Sin datos pendientes, las opciones visibles pasan a ser los atajos.
        conversation.opciones = {"paso": conversation.paso, "tipo": "botones", "cuerpo": body, "boton": None,
                                 "filas": button_rows("G5")}
    return body


async def _not_understood(sender: str, conversation: Conversation) -> str | None:
    """Regla G3/G4: la primera vez se reformula con las opciones; la segunda seguida pasa a una persona."""
    conversation.intentos_fallidos += 1
    if conversation.intentos_fallidos >= 2:
        conversation.intentos_fallidos = 0
        return await _handoff(sender, conversation, "no_entiende")
    shown = conversation.opciones or {}
    if shown.get("tipo") in _INTERACTIVOS:
        return await _resume(sender, conversation, "Uy, creo que no te entendí bien 🙈 ¿Me ayudas escogiendo una opción?")
    return await _show_step(sender, "G3", conversation)


async def _answer_and_resume(body: str, sender: str, conversation: Conversation) -> str | None:
    """Claude responde y la respuesta llega con las opciones del paso en curso (o con los atajos si no hay paso)."""
    reply = await ask_claude(body, conversation.historial)
    if not reply:
        logger.warning("Respuesta de Claude vacía: se retoma el paso")
        return await _resume(sender, conversation, "¿Me lo repites, por favor? 🌿")
    if reply.strip().upper().startswith(_NO_ENTIENDO):
        logger.info("Especia no entendió el mensaje (intento %d)", conversation.intentos_fallidos + 1)
        return await _not_understood(sender, conversation)
    if await _navigate(reply, body, sender, conversation):
        return None  # la ruta ya respondió y guardó el turno
    if _IR.match(reply):
        # Un destino no permitido: nunca se le muestra el código al cliente.
        logger.warning("Claude propuso un destino no permitido: se retoman las opciones")
        return await _resume(sender, conversation, "Te muestro las opciones para seguir 🌿")
    conversation.intentos_fallidos = 0
    if (conversation.opciones or {}).get("tipo") in _INTERACTIVOS:
        return await _resend(sender, conversation, reply)
    return await _shortcuts(sender, conversation, reply)


# ── Pedido: P6 o kits (qué y cuánto) → P7 (ciudad) → P7B/P7C (envío) → P8 (resumen) ─

_REINTENTO_TECNICO = "Uy, tuve un problema técnico leyendo tu mensaje 🙈 ¿Me lo repites, por favor?"
_DATOS_DEL_PEDIDO = ("pedido_id", "pedido", "kit", "envio_guardado")


def _clear_order(conversation: Conversation) -> None:
    """Empieza un pedido nuevo. Los datos de envío se conservan: si ya están, se va directo al resumen."""
    for key in _DATOS_DEL_PEDIDO:
        conversation.datos.pop(key, None)


async def _start_order(sender: str, conversation: Conversation) -> str | None:
    _clear_order(conversation)
    return await _show_step(sender, "P6", conversation)


async def _start_kit_mini(sender: str, conversation: Conversation) -> str | None:
    _clear_order(conversation)
    return await _show_step(sender, "P4M", conversation)


async def _start_kit(sender: str, option: dict, conversation: Conversation) -> str | None:
    _clear_order(conversation)
    size = option["kit"]
    conversation.datos["kit"] = size
    step = get_step("P4K")
    text = step["texto_todas"] if size >= len(ordered_mix_ids()) else step["texto"].replace("[tamaño]", str(size))
    return await _show_step(sender, "P4K", conversation, body=text)


async def _take_kit_mini(body: str, sender: str, conversation: Conversation) -> str | None:
    quantity = parse_quantity(body)
    if quantity is None:
        extraction = await extract_order(body, get_product("kit_mini")["nombre"])
        if extraction is None:
            return await _reply_text(sender, _REINTENTO_TECNICO)
        if extraction.quiere_distribuir:
            return await _handoff(sender, conversation, "distribuidor")
        quantity = sum(l.cantidad for l in extraction.lineas if l.producto == "kit_mini" and 1 <= l.cantidad <= 100) or None
        if quantity is None:
            if extraction.pregunta_o_comentario:
                return await _answer_and_resume(body, sender, conversation)
            return await _reply_text(sender, "¿Cuántos kits mini quieres? Escríbeme solo el número 🙌")
    return await _items_ready(sender, [{"producto": "kit_mini", "cantidad": quantity}], conversation)


def _kit_retry(kit_size: int, named: int) -> str:
    return (
        f"Tu kit es de {kit_size} mezclas y me nombraste {named} 🙈 ¿Cuáles {kit_size} quieres? Puedes escribirlas "
        "cortas: oro, sol, luna rosa, vid, chilemon o tierra de fuego."
    )


def _order_items(extraction, conversation: Conversation) -> tuple[list[dict] | None, str | None]:
    """Convierte la extracción en ítems válidos. Devuelve (ítems, None) o (None, mensaje para volver a preguntar)."""
    kit_size = conversation.datos.get("kit")
    lines = [(l.producto, l.cantidad) for l in extraction.lineas if get_product(l.producto) and 1 <= l.cantidad <= 100]
    if kit_size:
        kits = extraction.kits if extraction.kits and 1 <= extraction.kits <= 100 else 1
        if kit_size >= len(ordered_mix_ids()):
            return [{"kit": ordered_mix_ids(), "cantidad": kits}], None
        # Un kit no repite mezclas: se cuentan las distintas que nombró.
        mixes = list(dict.fromkeys(product_id for product_id, _ in lines if product_id in mix_ids()))
        if len(mixes) != kit_size:
            return None, _kit_retry(kit_size, len(mixes))
        return [{"kit": mixes, "cantidad": kits}], None
    if not lines:
        unknown = f" No tenemos {', '.join(extraction.no_reconocidos)}." if extraction.no_reconocidos else ""
        return None, (
            f"No logré identificar las mezclas 🙈{unknown} Tenemos Sal de oro, Sal de sol, Sal de luna rosa, "
            "Sal de la vid, Sal chilemon, Mezcla tierra de fuego y el Kit mini. ¿Cuáles quieres y cuántas?"
        )
    merged: dict[str, int] = {}
    for product_id, quantity in lines:
        merged[product_id] = merged.get(product_id, 0) + quantity
    return [{"producto": product_id, "cantidad": quantity} for product_id, quantity in merged.items()], None


async def _take_order_items(body: str, sender: str, conversation: Conversation) -> str | None:
    kit_size = conversation.datos.get("kit")
    if kit_size and kit_size >= len(ordered_mix_ids()) and (quantity := parse_quantity(body)):
        # El kit de 6 trae todas las mezclas: un «2» es cuántos kits.
        return await _items_ready(sender, [{"kit": ordered_mix_ids(), "cantidad": quantity}], conversation)
    named = [mix for mix in find_mixes(body) if mix in mix_ids()] if kit_size else []
    if kit_size and len(named) == kit_size:
        # Nombró justo las mezclas del kit, con el nombre completo o corto («oro, luna rosa, vid y tierra de
        # fuego»): se arma sin Claude.
        return await _items_ready(sender, [{"kit": named, "cantidad": count_kits(body) or 1}], conversation)
    extraction = await extract_order(body, None, kit_size)
    if extraction is None:
        if named:
            # Sin Claude, al menos se le dice cuántas reconocimos y cómo escribirlas.
            return await _reply_text(sender, _kit_retry(kit_size, len(named)))
        return await _reply_text(sender, _REINTENTO_TECNICO)
    if extraction.quiere_distribuir:
        return await _handoff(sender, conversation, "distribuidor")
    if extraction.pregunta_o_comentario and not extraction.lineas and not extraction.kits:
        # Una pregunta a mitad del pedido: se responde y se repite lo que falta para seguir donde iba.
        return await _answer_and_resume(body, sender, conversation)
    items, retry = _order_items(extraction, conversation)
    if items is None:
        return await _reply_text(sender, retry)
    return await _items_ready(sender, items, conversation)


async def _items_ready(sender: str, items: list[dict], conversation: Conversation) -> str | None:
    """Pedido listo: pasa a la ciudad de envío (P7) o, si ya se conocen los datos, al resumen."""
    conversation.datos["pedido"] = items
    conversation.datos.pop("kit", None)
    logger.info("Pedido capturado: %d ítem(s)", len(items))
    # Si ya tenemos los datos de envío (p. ej. el cliente tocó «Cambiar algo»), se va directo al resumen.
    if not missing_shipping_fields(conversation.datos.get("envio", {})):
        return await _show_summary(sender, conversation)
    return await _show_step(sender, "P7", conversation)


def _shipping_line(envio: dict) -> str:
    return f"{envio['nombre']}\n{envio['direccion']}, {envio['ciudad']}\nTel. {envio['telefono']}"


async def _extract_shipping_fields(body: str) -> tuple[dict | None, bool]:
    """(datos de envío encontrados, es una pregunta sin datos). None si la extracción falló."""
    extraction = await extract_shipping(body)
    if extraction is None:
        return None, False
    found = {field: value.strip() for field, value in extraction.model_dump(exclude={"pregunta_o_comentario"}).items()
             if value and value.strip()}
    return found, extraction.pregunta_o_comentario and not found


async def _take_city(body: str, sender: str, conversation: Conversation) -> str | None:
    found, question = await _extract_shipping_fields(body)
    if found is None:
        return await _reply_text(sender, _REINTENTO_TECNICO)
    if question:
        return await _answer_and_resume(body, sender, conversation)
    shipping = {**conversation.datos.get("envio", {}), **found}
    conversation.datos["envio"] = shipping
    if not shipping.get("ciudad"):
        return await _reply_text(sender, "¿A qué ciudad te lo enviamos? 🙌")
    if not missing_shipping_fields(shipping):
        # Escribió todos los datos de una vez: directo al resumen, que trae el valor del envío.
        return await _show_summary(sender, conversation)
    return await _shipping_info(sender, conversation)


async def _shipping_info(sender: str, conversation: Conversation) -> str | None:
    """P7B: valor y tiempo del envío a su ciudad, días de despacho y los datos que faltan. Si ya compró y su
    dirección guardada es de la misma ciudad, se le ofrece (P7C) en el mismo mensaje."""
    shipping = conversation.datos["envio"]
    city = shipping["ciudad"]
    step = get_step("P7B")
    far = shipping_zone(city) == "resto_del_pais"
    info = (
        step["texto"].replace("[ciudad]", city).replace("[valor del envío]", format_price(shipping_cost(city)))
        .replace("[tiempo de entrega]", step["tiempos"]["resto" if far else "cerca"])
    )
    if far:
        info += f" {step['aviso_resto']}"
    missing = missing_shipping_fields(shipping)
    saved = archivo.saved_shipping(sender)
    if (missing == ["nombre", "direccion", "telefono"] and saved and not missing_shipping_fields(saved)
            and same_city(saved["ciudad"], city)):
        conversation.datos["envio_guardado"] = saved
        texto = get_step("P7C")["texto"].replace("[dirección guardada]", _shipping_line(saved))
        return await _show_step(sender, "P7C", conversation, body=f"{info}\n\n{texto}")
    ask = step["pide_datos"] if missing == ["nombre", "direccion", "telefono"] else ask_missing_shipping(missing)
    return await _show_step(sender, "P7B", conversation, body=f"{info}\n\n{ask}")


async def _use_saved_shipping(sender: str, conversation: Conversation) -> str | None:
    saved = conversation.datos.pop("envio_guardado", None)
    if not saved or not conversation.datos.get("pedido"):
        # El pedido ya no está (p. ej. la conversación caducó): se vuelve a armar.
        return await _start_order(sender, conversation)
    conversation.datos["envio"] = saved
    return await _show_summary(sender, conversation)


async def _take_shipping(body: str, sender: str, conversation: Conversation) -> str | None:
    if conversation.paso == "P7C":
        # Escribió otra dirección en vez de tocar un botón: se toma esa.
        conversation.datos.pop("envio_guardado", None)
    found, question = await _extract_shipping_fields(body)
    if found is None:
        return await _reply_text(sender, _REINTENTO_TECNICO)
    if question:
        return await _answer_and_resume(body, sender, conversation)
    shipping = {**conversation.datos.get("envio", {}), **found}
    conversation.datos["envio"] = shipping
    missing = missing_shipping_fields(shipping)
    if missing:
        return await _reply_text(sender, ask_missing_shipping(missing))
    return await _show_summary(sender, conversation)


async def _show_summary(sender: str, conversation: Conversation) -> str | None:
    summary = order_summary(conversation.datos["pedido"], conversation.datos["envio"], get_step("P7B")["aviso_resto"])
    return await _show_step(sender, "P8", conversation, body=summary)


# ── Quejas (Q1 → Q2 o T1/T2) ──────────────────────────────────────────────────

async def _take_complaint(body: str, sender: str, conversation: Conversation) -> str | None:
    """Lo que el cliente contó del reclamo: si es grave (producto dañado, salud o cobro errado) pasa urgente con Q2."""
    conversation.datos["detalle"] = body
    serious = await is_serious_complaint(body)
    return await _handoff(sender, conversation, "queja_grave" if serious else "queja")


# ── Opciones tocadas ──────────────────────────────────────────────────────────

async def handle_choice(interactive: dict | None, sender: str, conversation: Conversation) -> None:
    if _bot_is_silenced(conversation):
        return
    conversation.intentos_fallidos = 0
    reply = interactive_reply(interactive)
    if reply is None:
        logger.info("Mensaje interactivo sin respuesta de lista o botón ignorado")
        return
    found = find_any_option(reply["id"])
    if found is None:
        # Opción de una lista vieja que ya no existe: se vuelve a mostrar el menú.
        logger.info("Opción desconocida '%s', se reenvía el menú", reply["id"])
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation)
        if sent:
            conversation.add_turn(f"Elegí «{reply.get('title', '')}»", sent)
        return

    step_id, option = found
    option_id, destination = option["id"], option.get("va_a")
    logger.info("Opción elegida: %s (%s → %s)", option_id, step_id, destination)

    if option_id == "atajo_menu":
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation)
    elif destination in _DIRECTOS:
        sent = await _handoff(sender, conversation, _DIRECTOS[destination])
    elif destination == "T1":
        sent = await _handoff(sender, conversation, "solicitud")
    elif "kit" in option:
        sent = await _start_kit(sender, option, conversation)
    elif option_id == "p4_kit_mini":
        sent = await _start_kit_mini(sender, conversation)
    elif destination == "P6":
        sent = await _start_order(sender, conversation)
    elif option_id == "p7c_misma":
        sent = await _use_saved_shipping(sender, conversation)
    elif option_id == "p7c_otra":
        conversation.datos.pop("envio_guardado", None)
        sent = await _show_step(sender, "P7D", conversation)
    elif option_id == "p8_confirmar":
        ready = conversation.datos.get("pedido") and not missing_shipping_fields(conversation.datos.get("envio", {}))
        sent = await (_show_step(sender, "P11", conversation) if ready else _start_order(sender, conversation))
    elif option_id == "p11_transferencia":
        sent = await _confirm_transfer(sender, conversation)
    elif option_id == "p11_link":
        sent = await _confirm_order(sender, conversation)
    elif option_id in ("p15_si", "p15_no"):
        sent = await _after_sale_answer(sender, option_id == "p15_si")
    elif destination in _PASOS_SIMPLES:
        sent = await _show_step(sender, destination, conversation)
    else:
        # Opción de un mensaje que Especia todavía no envía (p. ej. la postventa O4): el menú.
        logger.warning("Opción sin acción '%s': se muestra el menú", option_id)
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation)
    if sent:
        conversation.add_turn(f"Elegí «{option['titulo']}»", sent)


# ── Pago: link de Bold (P13) o transferencia (P12 → P14) → confirmado (P15) ──

def _fill(text: str, order: Pedido) -> str:
    return (
        text.replace("[referencia]", order.referencia)
        .replace("[total]", format_price(order.total))
        .replace("[link de pago]", order.link_url or "")
    )


def _remember(sender: str, conversation: Conversation, order: Pedido, items: list[dict], envio: dict) -> None:
    conversation.datos["pedido_id"] = order.id
    archivo.remember_order(
        sender, conversation.nombre, envio, order.referencia,
        "; ".join(describe_item(item).lstrip("• ") for item in items),
    )
    for etiqueta in ("Producto", "Pendiente de pago"):
        if etiqueta not in conversation.etiquetas:
            conversation.etiquetas.append(etiqueta)


async def _confirm_order(sender: str, conversation: Conversation) -> str | None:
    """«Link de pago»: guarda el pedido y envía el link de Bold (P13). Sin Bold, o si falla, pasa a una persona."""
    if not bold.is_enabled():
        return await _handoff(sender, conversation, "pedido")
    # Si el cliente vuelve a tocar «Link de pago», se reenvía el mismo link en vez de crear otro.
    previous = get_order(conversation.datos["pedido_id"]) if conversation.datos.get("pedido_id") else None
    if previous and previous.estado == PENDIENTE and previous.link_url:
        return await _show_step(sender, "P13", conversation, body=_fill(get_step("P13")["texto"], previous))
    _cancel_unpaid_transfer(previous)

    items, envio = conversation.datos.get("pedido"), conversation.datos.get("envio", {})
    if not items or missing_shipping_fields(envio):
        return await _handoff(sender, conversation, "pedido")
    order = create_order(sender, conversation.nombre, items, envio, subtotal(items), shipping_cost(envio["ciudad"]))
    link = await bold.create_payment_link(
        order.total, f"Pedido Saboryarte {order.referencia}", f"{order.referencia}-{int(order.creado_en)}",
    )
    if link is None:
        order.estado = REVISAR
        update_order(order)
        logger.warning("Pedido %s sin link de pago: pasa a una persona", order.referencia)
        return await _handoff(sender, conversation, "pedido")
    order.link_id, order.link_url, order.vence_en = link["id"], link["url"], link["vence_en"]
    update_order(order)
    logger.info("Pedido %s con link de pago %s", order.referencia, order.link_id)
    _remember(sender, conversation, order, items, envio)
    return await _show_step(sender, "P13", conversation, body=_fill(get_step("P13")["texto"], order))


def _cancel_unpaid_transfer(order: Pedido | None) -> None:
    """El cliente cambió de medio de pago antes de enviar el comprobante: ese pedido no se cobra."""
    if order and order.medio_pago == TRANSFERENCIA and order.estado == ESPERANDO_COMPROBANTE:
        order.estado = ANULADO
        update_order(order)


async def _confirm_transfer(sender: str, conversation: Conversation) -> str | None:
    """«Transferencia»: registra el pedido y da los datos de la cuenta (P12)."""
    previous = get_order(conversation.datos["pedido_id"]) if conversation.datos.get("pedido_id") else None
    if previous and previous.medio_pago == TRANSFERENCIA and previous.estado in (ESPERANDO_COMPROBANTE, POR_VERIFICAR):
        return await _show_step(sender, "P12", conversation, body=_fill(get_step("P12")["texto"], previous))
    items, envio = conversation.datos.get("pedido"), conversation.datos.get("envio", {})
    if not items or missing_shipping_fields(envio):
        return await _handoff(sender, conversation, "pedido")
    # Si había un link de Bold sin pagar para este mismo pedido, queda anulado: se paga por transferencia.
    if previous and previous.estado == PENDIENTE:
        previous.estado = ANULADO
        update_order(previous)
    order = create_order(sender, conversation.nombre, items, envio, subtotal(items), shipping_cost(envio["ciudad"]),
                         medio_pago=TRANSFERENCIA)
    logger.info("Pedido %s por transferencia: se espera el comprobante", order.referencia)
    _remember(sender, conversation, order, items, envio)
    return await _show_step(sender, "P12", conversation, body=_fill(get_step("P12")["texto"], order))


_TIPOS_SIN_COMPROBANTE = ("audio", "video", "sticker")


async def handle_media(kind: str, media: dict, sender: str, conversation: Conversation) -> None:
    """Foto, documento, audio, video o sticker del cliente. Una foto o un documento es el comprobante si tiene un
    pedido por transferencia pendiente; una foto durante una queja es la prueba del problema."""
    if _bot_is_silenced(conversation):
        return
    if conversation.paso == "Q1":
        conversation.datos["detalle"] = "Envió una foto o un archivo del problema (revísalo en el chat)."
        sent = await _handoff(sender, conversation, "queja_grave")
        if sent:
            conversation.add_turn("[Envié una foto del problema]", sent)
        return
    order = pending_transfer(sender) if kind not in _TIPOS_SIN_COMPROBANTE else None
    if order is None:
        sent = await (_reply_text(sender, get_step("P14A")["texto"]) if kind == "document"
                      else _media_not_supported(kind, sender, conversation))
        if sent:
            conversation.add_turn(f"[Envié {'un archivo' if kind == 'document' else 'un ' + kind}]", sent)
        return
    mime = (media.get("mime_type") or "").split(";")[0].strip()
    downloaded = await download_media(media["id"]) if mime in transferencias.TIPOS_COMPROBANTE else None
    if downloaded is None or downloaded[1] not in transferencias.TIPOS_COMPROBANTE:
        await _reply_text(sender, get_step("P14P")["texto"])
        return
    path = transferencias.save_receipt(order.referencia, *downloaded)
    order.estado = POR_VERIFICAR
    order.comprobante = str(path)
    update_order(order)
    logger.info("Comprobante recibido para el pedido %s", order.referencia)
    step = get_step("P14")
    text = step["texto"] if is_open(_now()) else f"{step['texto']} {step['fuera_de_horario']}"
    sent = await _reply_text(sender, _fill(text, order))
    if sent:
        conversation.add_turn("[Envié el comprobante de la transferencia]", sent)
    buttons = transferencias.team_buttons(order.id)
    instrucciones = (
        "Revisa en Bancolombia que la transferencia llegó por el total y toca un botón: Especia le avisa al cliente."
        if buttons else
        "Revisa en Bancolombia que la transferencia llegó por el total. Para confirmarla en el bot: "
        f"python -m app.transferencias confirmar {order.referencia} (faltan PUBLIC_BASE_URL y TRANSFER_LINK_SECRET "
        "para tener botones en este correo)."
    )
    await notify_team(
        f"[Saboryarte] 🧾 Verificar transferencia — pedido {order.referencia} por {format_price(order.total)}",
        order_ficha(order, instrucciones), attachments=[path], buttons=buttons,
    )


async def _media_not_supported(kind: str, sender: str, conversation: Conversation) -> str | None:
    """O2: se recibió y no se puede ver o escuchar; se retoma el paso en curso o se muestra el menú."""
    step = get_step("O2")
    tipo, emoji, verbo = step["tipos"].get(kind, step["tipos"]["image"])
    text = step["texto"].replace("[tipo]", tipo).replace("[emoji]", emoji).replace("[verbo]", verbo)
    if conversation.flujo_activo and conversation.opciones:
        return await _resume(sender, conversation, text)
    return await _show_step(sender, MENU_PRINCIPAL, conversation, body=text)


async def resolve_transfer(order: Pedido, accion: str) -> str:
    """Acción del equipo desde el correo. Devuelve el mensaje para la página de confirmación."""
    if order.medio_pago != TRANSFERENCIA:
        return f"El pedido {order.referencia} no es por transferencia."
    if order.estado == PAGADO:
        return f"El pago del pedido {order.referencia} ya estaba confirmado. No se hizo nada más."
    if order.estado not in (ESPERANDO_COMPROBANTE, POR_VERIFICAR):
        return f"El pedido {order.referencia} en estado «{ESTADOS.get(order.estado, order.estado)}»: no se puede cambiar desde aquí."
    if accion == transferencias.RECHAZAR:
        order.estado = ESPERANDO_COMPROBANTE
        update_order(order)
        # P16: Juanita revisa el pago con el cliente. El bot se silencia; no hace falta otro correo al equipo.
        conversation = load_conversation(order.remitente)
        sent = await _handoff(order.remitente, conversation, "pago", notify=False)
        save_conversation(conversation)
        aviso = "Especia le avisó al cliente que revisas el pago con él. La conversación queda en tus manos."
        return aviso if sent else (
            "WhatsApp no dejó avisarle al cliente (pasaron más de 24 h desde su último mensaje): "
            "escríbele desde la app de WhatsApp Business.")
    order.estado = PAGADO
    order.pagado_en = time.time()
    update_order(order)
    sent = await _payment_confirmed(order)
    archivo.remember_payment(order.remitente, order.referencia)
    archivo.save_order_chat(order.id, order.referencia, order.remitente, order.nombre, order_ficha(order))
    await notify_team(
        f"[Saboryarte] 💰 Pedido pagado {order.referencia} — {order.nombre or order.remitente}: despachar",
        order_ficha(order, "Pago por transferencia confirmado por el equipo."),
    )
    aviso = f"Listo: el pedido {order.referencia} quedó pagado."
    return f"{aviso} Le avisamos al cliente por WhatsApp." if sent else (
        f"{aviso} WhatsApp no dejó avisarle al cliente (pasaron más de 24 h desde su último mensaje): "
        "escríbele desde la app de WhatsApp Business.")


async def _payment_confirmed(order: Pedido) -> bool:
    """P15: pago confirmado, día de despacho y la pregunta de la postventa."""
    conversation = load_conversation(order.remitente)
    paid = datetime.fromtimestamp(order.pagado_en or time.time(), tz=COLOMBIA)
    body = _fill(get_step("P15")["texto"], order).replace("[día de despacho]", spanish_date(dispatch_day(paid)))
    sent = await _show_step(order.remitente, "P15", conversation, body=body)
    if "Pendiente de pago" in conversation.etiquetas:
        conversation.etiquetas.remove("Pendiente de pago")
    for etiqueta in ("Producto", "Pagado"):
        if etiqueta not in conversation.etiquetas:
            conversation.etiquetas.append(etiqueta)
    save_conversation(conversation)
    return bool(sent)


async def _after_sale_answer(sender: str, accepted: bool) -> str | None:
    """Respuesta a P15: se guarda en el último pedido pagado (permiso para el mensaje de postventa)."""
    order = last_paid(sender)
    if order:
        order.postventa = 1 if accepted else 0
        update_order(order)
        logger.info("Pedido %s: postventa %s", order.referencia, "aceptada" if accepted else "rechazada")
    return await _reply_text(sender, get_step("P15S" if accepted else "P15N")["texto"])


async def handle_bold_event(event: dict) -> None:
    """Aviso del webhook de Bold, ya con la firma verificada. Bold puede repetir avisos: se procesan una vez."""
    kind = event.get("type")
    data = event.get("data") or {}
    # Según Bold, en pagos con link la referencia es el id del link (LNK_…). Por si llega la referencia que
    # enviamos al crear el link («SAB-00001-<timestamp>»), también se busca por ella.
    reference = (data.get("metadata") or {}).get("reference") or ""
    total = (data.get("amount") or {}).get("total")
    logger.info("Bold: aviso %s recibido (referencia %s, total %s, pago %s)", kind, reference, total, data.get("payment_id"))
    order = (find_by_link(reference) or find_by_reference(reference)) if reference else None
    if order is None:
        logger.warning("Bold: aviso %s sin pedido asociado (referencia %s)", kind, reference or "vacía")
        return
    # Se marca solo si corresponde a un pedido: un aviso sin pedido puede reintentarse cuando se corrija la causa.
    if not mark_processed(f"bold:{event.get('id')}"):
        logger.info("Bold: aviso repetido ignorado")
        return
    logger.info("Bold: aviso %s para el pedido %s", kind, order.referencia)
    if kind == "SALE_APPROVED":
        await _payment_approved(order, data)
    elif kind == "SALE_REJECTED":
        if order.estado == PENDIENTE:
            await send_text(order.remitente, _fill(get_step("P13R")["texto"], order))
    elif kind == "VOID_APPROVED":
        order.estado = ANULADO
        update_order(order)
        await notify_team(f"[Saboryarte] Pago anulado — pedido {order.referencia}", order_ficha(order))


async def _payment_approved(order: Pedido, data: dict) -> None:
    if order.estado == PAGADO:
        return
    order.payment_id = data.get("payment_id")
    reported = (data.get("amount") or {}).get("total")
    # Segunda verificación además de la firma: Bold debe reportar el link como pagado y por el monto del pedido.
    # En el ambiente de pruebas los pagos no cierran el link (queda en PROCESSING): ahí solo se verifica el monto.
    if settings.bold_sandbox:
        status = "PAID (sin verificar: ambiente de pruebas)"
        link_ok = True
    else:
        status = await bold.confirm_paid(order.link_id)
        link_ok = status == "PAID"
    if not link_ok or reported is None or int(reported) != order.total:
        order.estado = REVISAR
        update_order(order)
        logger.warning("Bold: pago del pedido %s por revisar (link %s)", order.referencia, status)
        await notify_team(
            f"[Saboryarte] ⚠️ Revisar pago — pedido {order.referencia}",
            order_ficha(order, f"Bold avisó un pago aprobado por {reported}, pero el link figura como {status}. "
                               "Revísalo en el panel de Bold antes de despachar."),
        )
        return
    order.estado = PAGADO
    order.pagado_en = time.time()
    update_order(order)
    await _payment_confirmed(order)
    archivo.remember_payment(order.remitente, order.referencia)
    # Chat completo + ficha: se guarda 1 año y se copia a Google Drive para que el equipo lo lea.
    archivo.save_order_chat(order.id, order.referencia, order.remitente, order.nombre, order_ficha(order))
    await notify_team(
        f"[Saboryarte] 💰 Pedido pagado {order.referencia} — {order.nombre or order.remitente}: despachar",
        order_ficha(order),
    )


# ── Traspaso a una persona (coexistencia con la app de WhatsApp Business) ────

# Mensaje al cliente según el motivo: (dentro de horario, fuera de horario). Los demás usan T1/T2.
_MENSAJES_DE_TRASPASO = {
    "pedido": ("P13H", "P13F"),
    "corporativo": ("R1", "R1F"),
    "distribuidor": ("D1", "D1F"),
    "proveedor": ("V1", "V1F"),
    "pago": ("P16", "P16F"),
}


async def _handoff(sender: str, conversation: Conversation, motivo: str, notify: bool = True) -> str | None:
    """Avisa al cliente, silencia el bot y envía la ficha al equipo."""
    if motivo == "no_entiende":
        step = "G4"
    elif motivo == "queja_grave":
        step = "Q2"
    else:
        inside, outside = _MENSAJES_DE_TRASPASO.get(motivo, ("T1", "T2"))
        step = inside if is_open(_now()) else outside
    sent = await _show_step(sender, step, conversation)
    if not sent:
        return None
    start_handoff(conversation, motivo)
    logger.info("Traspaso iniciado (motivo: %s, prioridad: %s)", motivo, conversation.traspaso["prioridad"])
    if notify:
        urgente = "🚨 URGENTE · " if conversation.traspaso["prioridad"] == "urgente" else ""
        await notify_team(
            f"[Saboryarte] {urgente}{MOTIVOS[motivo]} — {conversation.nombre or conversation.remitente}",
            handoff_ficha(conversation),
        )
    return sent


async def handle_team_message(text: str, customer: str, conversation: Conversation) -> None:
    """Mensaje que alguien del equipo escribió desde la app (eco de coexistencia)."""
    if is_closing_message(text):
        if conversation.en_traspaso:
            logger.info("El equipo cerró el traspaso: el bot retoma la conversación")
            end_handoff(conversation)
            await send_text(customer, get_step("T3")["texto"])
        return
    if conversation.en_traspaso:
        register_team_message(conversation)
    else:
        # El equipo intervino por su cuenta: el bot se calla para no responder encima de la persona.
        logger.info("El equipo escribió desde la app: se inicia el traspaso")
        start_handoff(conversation, "equipo_intervino")


async def check_unanswered_handoffs(now: datetime | None = None) -> None:
    """Alerta al equipo por los traspasos que nadie ha respondido a tiempo (en horario de atención)."""
    now = now or _now()
    for conversation in list_handoffs():
        if not needs_alert(conversation, now):
            continue
        started = datetime.fromtimestamp(conversation.traspaso["inicio"], tz=COLOMBIA)
        minutes = int(business_minutes_between(started, now))
        await notify_team(
            f"[Saboryarte] ⏰ Sin respuesta hace {minutes} min — {conversation.nombre or conversation.remitente}",
            handoff_ficha(conversation),
        )
        # Se marca aunque el correo falle, para no repetir la alerta cada minuto.
        conversation.traspaso["alerta_enviada"] = True
        save_conversation(conversation)
