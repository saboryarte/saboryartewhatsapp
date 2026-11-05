"""Decide qué responder a cada mensaje según el estado de la conversación y los pasos de mensajes.json."""
import logging
import re
import time
from datetime import datetime

from .claude import ask_claude, extract_order, extract_shipping
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
    product_card,
    step_options,
    recommendation,
    under_construction,
)
from .greetings import is_greeting, is_human_request, is_menu_request, normalize_text, pick_welcome
from .handoff import (
    COLOMBIA,
    MOTIVOS,
    business_minutes_between,
    end_handoff,
    handoff_ficha,
    is_closing_message,
    is_open,
    needs_alert,
    register_team_message,
    start_handoff,
    team_went_idle,
)
from . import archivo, bold
from .notify import notify_team
from .orders import (
    ask_missing_shipping,
    describe_item,
    merge_items,
    missing_shipping_fields,
    order_summary,
    parse_quantity,
    shipping_cost,
    subtotal,
)
from .config import settings
from .prompt import DESTINOS
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
    order_ficha,
    pending_transfer,
    update_order,
)
from . import transferencias
from .state import Conversation, list_handoffs, mark_processed, save_conversation
from .whatsapp import download_media, flow_response, interactive_reply, send_buttons, send_flow, send_list, send_text

logger = logging.getLogger(__name__)

# Pasos que el bot ya sabe mostrar. Una opción que lleva a cualquier otro responde «en construcción».
PASOS_DISPONIBLES = frozenset({
    "G1", "P1", "P2", "P2F", "P3", "P4", "P4B", "P5", "P6", "P7", "P7G", "P8", "P9", "P9B", "P9C", "P9O", "P9S", "O1",
})
# Respuestas a una duda: después se pregunta «¿Te sirvió?» (P9S).
_RESPUESTAS_DE_DUDA = frozenset({"P9B", "P9C", "O1"})
# Pasos de otras rutas que se usan como respuesta dentro de la ruta actual: no cambian el flujo activo.
_FLUJOS_DE_APOYO = frozenset({"entrada", "otras"})
# Menú principal: G1 dentro del horario de atención, G2 (con aviso de horario) fuera.
_MENUS = frozenset({MENU_PRINCIPAL, "G2"})


def _now() -> datetime:
    return datetime.now(COLOMBIA)


def _menu_step() -> str:
    return MENU_PRINCIPAL if is_open(_now()) else "G2"


async def _show_step(
    sender: str, step_id: str, conversation: Conversation, body: str | None = None, rows: list[dict] | None = None,
) -> str | None:
    """Envía un paso según su tipo y actualiza el estado. Devuelve el texto enviado, o None si Meta lo rechazó.
    «rows» reemplaza las filas de una lista (p. ej. para quitar las mezclas ya elegidas)."""
    step = get_step(step_id)
    text = body or step["texto"]
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
    if step_id in _MENUS:
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
        sent = await _show_step(sender, _menu_step(), conversation)
    elif option := _typed_option(body, conversation):
        # Escribir el texto de una opción visible cuenta como tocarla.
        logger.info("Texto reconocido como la opción '%s'", option["id"])
        await handle_choice({"type": "list_reply", "list_reply": option}, sender, conversation)
        return
    elif _asks_to_see_options(body, conversation):
        sent = await _resend(sender, conversation, conversation.opciones["cuerpo"])
    elif conversation.flujo_activo is None and is_greeting(body):
        logger.info("Saludo detectado, se envía el menú")
        menu = _menu_step()
        welcome = pick_welcome(profile_name) if menu == MENU_PRINCIPAL else None
        sent = await _show_step(sender, menu, conversation, body=welcome)
    elif is_greeting(body):
        # Un saludo a mitad de un paso no lo reinicia: se retoma donde iba, con sus opciones.
        sent = await _resume(sender, conversation, "¡Hola de nuevo! 🌿 Sigamos donde íbamos.")
    elif conversation.paso == "P6Q" and (quantity := parse_quantity(body)):
        sent = await _add_to_cart(sender, quantity, conversation)
    elif conversation.paso in ("P6", "P6Q", "P6M", "P4C", "P4F"):
        # En los pasos con lista o botones el cliente también puede escribir lo que quiere.
        sent = await _take_order_items(body, sender, conversation)
    elif conversation.paso in ("P7", "P7G"):
        # En P7G el cliente puede escribir otra dirección en vez de tocar un botón: se toma desde cero.
        if conversation.paso == "P7G":
            conversation.datos.pop("envio_guardado", None)
        sent = await _take_shipping(body, sender, conversation)
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
        return await _show_step(sender, _menu_step(), conversation)
    if await _navigate(reply, sender, conversation):
        return None
    if _IR.match(reply):
        return await _show_step(sender, _menu_step(), conversation)
    if len(reply) <= _MAX_CUERPO_LISTA:
        return await _show_step(sender, _menu_step(), conversation, body=reply)
    sent = await _reply_text(sender, reply)
    return "\n\n".join(filter(None, [sent, await _show_step(sender, _menu_step(), conversation)]))


# ── Nunca dejar al cliente sin opciones ───────────────────────────────────────

_INTERACTIVOS = ("lista", "botones", "formulario")
_NO_ENTIENDO = "NO_ENTIENDO"
_IR = re.compile(r"^\s*IR:\s*([a-z0-9_]+)\s*$", re.IGNORECASE)


async def _navigate(reply: str, sender: str, conversation: Conversation) -> bool:
    """Si Claude respondió «IR:<opción>», lleva al cliente a esa opción como si la hubiera tocado."""
    match = _IR.match(reply)
    found = find_any_option(match.group(1).lower()) if match else None
    if not found or found[1]["id"] not in DESTINOS:
        return False
    option = found[1]
    logger.info("Texto llevado a la opción '%s'", option["id"])
    await handle_choice({"type": "list_reply", "list_reply": {"id": option["id"], "title": option["titulo"]}}, sender, conversation)
    return True


def _join(first: str, second: str | None, limit: int = _MAX_CUERPO_LISTA) -> str:
    """Une dos textos en un mensaje si caben en el límite; si no, solo el primero."""
    joined = f"{first}\n\n{second}" if second else first
    return joined if len(joined) <= limit else first


def _typed_option(body: str, conversation: Conversation) -> dict | None:
    """La opción visible cuyo texto escribió el cliente («Sal de oro», «2», «chilemon»), si es una sola."""
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


def _asks_to_see_options(body: str, conversation: Conversation) -> bool:
    """El cliente escribió el botón que abre las opciones («Ver mezclas», «Elegir mezclas»…)."""
    shown = conversation.opciones or {}
    if shown.get("tipo") not in _INTERACTIVOS:
        return False
    labels = {shown.get("boton") or "", get_step("P4F")["boton_formulario"] if shown.get("tipo") == "formulario" else ""}
    return normalize_text(body) in {normalize_text(label) for label in labels if label}


async def _resend(sender: str, conversation: Conversation, body: str) -> str | None:
    """Vuelve a enviar las últimas opciones (lista, botones o formulario) con otro texto. No cambia el paso."""
    shown = conversation.opciones
    if not shown:
        return await _show_step(sender, _menu_step(), conversation, body=body)
    limit = _MAX_CUERPO_LISTA if shown["tipo"] in _INTERACTIVOS else 4096
    if len(body) > limit:
        # Texto demasiado largo para ir con las opciones: va aparte y luego las opciones con su texto original.
        first = await _reply_text(sender, body)
        return _join(first or "", await _resend(sender, conversation, shown["cuerpo"]), 10_000) or None
    if shown["tipo"] == "lista":
        sent = await send_list(sender, body, shown["boton"], shown["filas"])
    elif shown["tipo"] == "botones":
        sent = await send_buttons(sender, body, shown["filas"])
    elif shown["tipo"] == "formulario":
        return await _send_kit_flow(sender, shown["tamaño"], conversation, body=body)
    else:
        sent = await send_text(sender, body)
    if sent:
        conversation.opciones = {**shown, "cuerpo_enviado": body}
    return body if sent else None


async def _resume(sender: str, conversation: Conversation, intro: str) -> str | None:
    """Retoma el paso en curso: el aviso más la pregunta y las opciones que el cliente tenía a la vista."""
    shown = conversation.opciones
    if not shown:
        return await _show_step(sender, _menu_step(), conversation)
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
    if await _navigate(reply, sender, conversation):
        return None  # handle_choice ya respondió y guardó el turno
    if _IR.match(reply):
        # Un destino no permitido: nunca se le muestra el código al cliente.
        logger.warning("Claude propuso un destino no permitido: se retoman las opciones")
        return await _resume(sender, conversation, "Te muestro las opciones para seguir 🌿")
    conversation.intentos_fallidos = 0
    if conversation.paso == "P9O":
        # Después de responder una duda libre, se pregunta si sirvió.
        return await _show_step(sender, "P9S", conversation, body=_join(reply, get_step("P9S")["texto"]))
    if (conversation.opciones or {}).get("tipo") in _INTERACTIVOS:
        return await _resend(sender, conversation, reply)
    return await _shortcuts(sender, conversation, reply)


# ── Pedido: P6 (qué y cuánto) → P7 (envío) → P8 (resumen) ─────────────────────

_REINTENTO_TECNICO = "Uy, tuve un problema técnico leyendo tu mensaje 🙈 ¿Me lo repites, por favor?"
_DISTRIBUIR = (
    "¡Qué bueno que quieras llevar Saboryarte a tu negocio! 🌿 Para distribuidores tenemos una ruta especial "
    "que todavía está en construcción.\n\nMientras tanto, si es para ti, cuéntame qué mezclas quieres y cuántas."
)


def _product_rows() -> list[dict]:
    """Filas de P6 con el precio del catálogo delante de la descripción (así no se desactualiza)."""
    rows = []
    for op in step_options("P6"):
        product = get_product(op["agregar"])
        rows.append({"id": op["id"], "title": op["titulo"], "description": f"{format_price(product['precio'])} · {op['descripcion']}"})
    return rows


async def _start_order(sender: str, option: dict, conversation: Conversation) -> str | None:
    """Empieza un pedido nuevo: con un producto ya elegido pregunta la cantidad; si no, muestra la lista."""
    for key in ("pedido_id", "pedido", "kit", "kit_mezclas", "carrito", "producto_pendiente"):
        conversation.datos.pop(key, None)
    product_id = option.get("producto") or (conversation.datos.get("producto") if option["id"] == "p2f_agregar" else None)
    if product_id and get_product(product_id):
        return await _ask_quantity(sender, product_id, conversation)
    return await _show_step(sender, "P6", conversation, rows=_product_rows())


async def _ask_quantity(sender: str, product_id: str, conversation: Conversation) -> str | None:
    product = get_product(product_id)
    conversation.datos["producto_pendiente"] = product_id
    body = (
        get_step("P6Q")["texto"]
        .replace("[producto]", f"*{product['nombre']}*")
        .replace("[precio]", format_price(product["precio"]))
    )
    return await _show_step(sender, "P6Q", conversation, body=body)


async def _add_to_cart(sender: str, quantity: int, conversation: Conversation) -> str | None:
    product_id = conversation.datos.pop("producto_pendiente", None)
    if not product_id:
        # Botón de cantidad de un mensaje viejo: se vuelve a mostrar la lista.
        return await _show_step(sender, "P6", conversation, rows=_product_rows())
    cart = merge_items(conversation.datos.get("carrito", []), [{"producto": product_id, "cantidad": quantity}])
    conversation.datos["carrito"] = cart
    lines = "\n".join(describe_item(item) for item in cart) + f"\nSubtotal: {format_price(subtotal(cart))}"
    return await _show_step(sender, "P6M", conversation, body=get_step("P6M")["texto"].replace("[carrito]", lines))


async def _finish_cart(sender: str, conversation: Conversation) -> str | None:
    cart = conversation.datos.get("carrito")
    if not cart:
        return await _show_step(sender, "P6", conversation, rows=_product_rows())
    return await _items_ready(sender, cart, conversation)


def _order_items(extraction, conversation: Conversation) -> tuple[list[dict] | None, str | None]:
    """Convierte la extracción en ítems válidos. Devuelve (ítems, None) o (None, mensaje para volver a preguntar)."""
    kit_size = conversation.datos.get("kit")
    lines = [(l.producto, l.cantidad) for l in extraction.lineas if get_product(l.producto) and 1 <= l.cantidad <= 100]
    if kit_size:
        mixes = [product_id for product_id, quantity in lines if product_id in mix_ids() for _ in range(quantity)]
        if len(mixes) != kit_size:
            return None, (
                f"Tu kit es de {kit_size} mezclas y me nombraste {len(mixes)} 🙈 "
                f"¿Cuáles {kit_size} mezclas quieres? Pueden ser Sal de oro, Sal de sol, Sal de luna rosa, "
                "Sal de la vid, Sal chilemon o Mezcla tierra de fuego."
            )
        return [{"kit": mixes}], None
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
    # Si se le estaba preguntando la cantidad de un producto, un «tres» se refiere a ese producto.
    selected = get_product(conversation.datos.get("producto_pendiente", ""))
    extraction = await extract_order(body, selected["nombre"] if selected else None, conversation.datos.get("kit"))
    if extraction is None:
        return await _resume(sender, conversation, _REINTENTO_TECNICO)
    if extraction.quiere_distribuir:
        return await _resume(sender, conversation, _DISTRIBUIR)
    if extraction.pregunta_o_comentario and not extraction.lineas:
        # Una pregunta a mitad del pedido: se responde con las opciones del paso para seguir donde iba.
        return await _answer_and_resume(body, sender, conversation)
    items, retry = _order_items(extraction, conversation)
    if items is None:
        return await _resume(sender, conversation, retry)
    if not conversation.datos.get("kit") and conversation.datos.get("carrito"):
        # Lo que escribe se suma a lo que ya eligió de la lista.
        items = merge_items(conversation.datos["carrito"], items)
    return await _items_ready(sender, items, conversation)


async def _items_ready(sender: str, items: list[dict], conversation: Conversation, intro: str | None = None) -> str | None:
    """Pedido listo: pasa a los datos de envío (P7) o, si ya se conocen, al resumen.
    «intro» se antepone a la pregunta de envío en el mismo mensaje (sin agregar un paso)."""
    conversation.datos["pedido"] = items
    for key in ("kit_mezclas", "carrito", "producto_pendiente"):
        conversation.datos.pop(key, None)
    logger.info("Pedido capturado: %d ítem(s)", len(items))
    # Si ya tenemos los datos de envío (p. ej. el cliente tocó «Cambiar algo»), se va directo al resumen.
    if not missing_shipping_fields(conversation.datos.get("envio", {})):
        return await _show_summary(sender, conversation)
    # Cliente que vuelve: se le ofrecen los datos de envío de su último pedido en vez de pedirlos otra vez.
    saved = archivo.saved_shipping(sender)
    if saved and not missing_shipping_fields(saved):
        conversation.datos["envio_guardado"] = saved
        texto = get_step("P7G")["texto"].replace("[datos de envío guardados]", _shipping_line(saved))
        return await _show_step(sender, "P7G", conversation, body=f"{intro} {texto}" if intro else texto)
    body = f"{intro} {get_step('P7')['texto']}" if intro else None
    return await _show_step(sender, "P7", conversation, body=body)


def _shipping_line(envio: dict) -> str:
    return f"{envio['nombre']}\n{envio['direccion']}, {envio['ciudad']}\nTel. {envio['telefono']}"


async def _use_saved_shipping(sender: str, conversation: Conversation) -> str | None:
    saved = conversation.datos.pop("envio_guardado", None)
    if not saved or not conversation.datos.get("pedido"):
        # El pedido ya no está (p. ej. la conversación caducó): se vuelve a armar desde la lista.
        return await _show_step(sender, "P6", conversation, rows=_product_rows())
    conversation.datos["envio"] = saved
    return await _show_summary(sender, conversation)


# ── Kit armado: elegir las mezclas con una lista (P4B → P4C) ──────────────────

async def _start_kit(sender: str, option: dict, conversation: Conversation) -> str | None:
    for key in ("pedido_id", "pedido", "producto"):
        conversation.datos.pop(key, None)
    size = option["kit"]
    conversation.datos["kit"] = size
    conversation.datos["kit_mezclas"] = []
    if size >= len(ordered_mix_ids()):
        # El kit de 6 trae todas las mezclas: no hay nada que elegir y no se agrega ningún paso.
        return await _items_ready(
            sender, [{"kit": ordered_mix_ids()}], conversation, intro=f"¡Tu kit de {size} trae todas nuestras mezclas! ✨",
        )
    if settings.whatsapp_kit_flow_id:
        sent = await _send_kit_flow(sender, size, conversation)
        if sent:
            return sent
        logger.warning("No se pudo enviar el Flow del kit: se usa la lista de una en una")
    return await _show_kit_picker(sender, conversation)


async def _send_kit_flow(sender: str, size: int, conversation: Conversation, body: str | None = None) -> str | None:
    """Formulario con casillas: WhatsApp exige marcar exactamente «size» mezclas."""
    step = get_step("P4F")
    prompt = step["texto"].replace("[tamaño]", str(size))
    body = body or prompt
    data = {
        "cantidad": size,
        "titulo": f"Elige {size} mezclas",
        "ayuda": f"Marca exactamente {size}. Cada una viene en 100 g (tierra de fuego en 60 g).",
        "mezclas": [{"id": op["mezcla"], "title": op["titulo"], "description": op["descripcion"]} for op in step_options("P4C")],
    }
    sent = await send_flow(
        sender, body, step["boton_formulario"], settings.whatsapp_kit_flow_id, f"kit-{size}", "KIT", data,
        settings.whatsapp_flows_mode,
    )
    if not sent:
        return None
    if conversation.paso != "P4F":
        conversation.add_step("P4F")
    conversation.paso = "P4F"
    conversation.opciones = {"paso": "P4F", "tipo": "formulario", "cuerpo": prompt, "boton": step["boton_formulario"],
                             "filas": None, "tamaño": size}
    return body


async def _kit_flow_reply(sender: str, response: dict, conversation: Conversation) -> str | None:
    """Respuesta del Flow del kit: las mezclas marcadas. Se valida en código aunque WhatsApp ya exige la cantidad."""
    token = str(response.get("flow_token") or "")
    size = conversation.datos.get("kit") or (int(token.split("-")[1]) if token.startswith("kit-") and token[4:].isdigit() else None)
    mixes = response.get("mezclas") or []
    valid = size and len(mixes) == size == len(set(mixes)) and all(m in mix_ids() for m in mixes)
    if not valid:
        logger.warning("Respuesta del Flow del kit inválida (%d mezclas para un kit de %s)", len(mixes), size)
        return await _show_step(sender, "P4B", conversation)
    conversation.datos["kit"] = size
    return await _items_ready(sender, [{"kit": list(mixes)}], conversation)


async def _show_kit_picker(sender: str, conversation: Conversation) -> str | None:
    size, chosen = conversation.datos["kit"], conversation.datos["kit_mezclas"]
    rows = [
        {"id": op["id"], "title": op["titulo"], "description": op["descripcion"]}
        for op in step_options("P4C") if op["mezcla"] not in chosen
    ]
    prompt = get_step("P4C")["texto"].replace("[n]", str(len(chosen) + 1)).replace("[tamaño]", str(size))
    if chosen:
        prefix = f"Llevas: {', '.join(get_product(m)['nombre'] for m in chosen)}."
    else:
        prefix = f"¡Arma tu kit de {size} mezclas! 🎁 Toca «Ver mezclas» y elige una por una."
    return await _show_step(sender, "P4C", conversation, body=f"{prefix}\n{prompt}", rows=rows)


async def _pick_kit_mix(sender: str, option: dict, conversation: Conversation) -> str | None:
    size = conversation.datos.get("kit")
    chosen = conversation.datos.get("kit_mezclas")
    if not size or chosen is None:
        # Una lista vieja: no hay un kit en curso, se vuelve a ofrecer el tamaño.
        return await _show_step(sender, "P4B", conversation)
    if option["mezcla"] not in chosen:
        chosen.append(option["mezcla"])
    if len(chosen) < size:
        return await _show_kit_picker(sender, conversation)
    return await _items_ready(sender, [{"kit": list(chosen)}], conversation)


async def _take_shipping(body: str, sender: str, conversation: Conversation) -> str | None:
    extraction = await extract_shipping(body)
    if extraction is None:
        return await _reply_text(sender, _REINTENTO_TECNICO)
    found = {field: value.strip() for field, value in extraction.model_dump(exclude={"pregunta_o_comentario"}).items() if value and value.strip()}
    if extraction.pregunta_o_comentario and not found:
        return await _answer_and_resume(body, sender, conversation)
    shipping = {**conversation.datos.get("envio", {}), **found}
    conversation.datos["envio"] = shipping
    missing = missing_shipping_fields(shipping)
    if missing:
        return await _reply_text(sender, ask_missing_shipping(missing))
    return await _show_summary(sender, conversation)


async def _show_summary(sender: str, conversation: Conversation) -> str | None:
    summary = order_summary(conversation.datos["pedido"], conversation.datos["envio"])
    return await _show_step(sender, "P8", conversation, body=summary)


async def handle_choice(interactive: dict | None, sender: str, conversation: Conversation) -> None:
    if _bot_is_silenced(conversation):
        return
    conversation.intentos_fallidos = 0
    response = flow_response(interactive)
    if response is not None:
        sent = await _kit_flow_reply(sender, response, conversation)
        if sent:
            names = ", ".join(get_product(m)["nombre"] for m in response.get("mezclas", []) if get_product(m))
            conversation.add_turn(f"Elegí para mi kit: {names}", sent)
        return
    reply = interactive_reply(interactive)
    if reply is None:
        logger.info("Mensaje interactivo sin respuesta de lista o botón ignorado")
        return
    found = find_any_option(reply["id"])
    if found is None:
        # Opción de una lista vieja que ya no existe: se vuelve a mostrar el menú.
        logger.info("Opción desconocida '%s', se reenvía el menú", reply["id"])
        sent = await _show_step(sender, _menu_step(), conversation)
        if sent:
            conversation.add_turn(f"Elegí «{reply.get('title', '')}»", sent)
        return

    step_id, option = found
    destination = option.get("va_a")
    logger.info("Opción elegida: %s (%s → %s)", option["id"], step_id, destination)
    if "producto" in option:
        conversation.datos["producto"] = option["producto"]

    parts: list[str | None] = []
    if option["id"] == "atajo_menu":
        parts.append(await _show_step(sender, _menu_step(), conversation))
    elif "mezcla" in option:
        parts.append(await _pick_kit_mix(sender, option, conversation))
    elif "agregar" in option:
        parts.append(await _ask_quantity(sender, option["agregar"], conversation))
    elif "cantidad" in option:
        parts.append(await _add_to_cart(sender, option["cantidad"], conversation))
    elif option["id"] == "carrito_agregar":
        parts.append(await _show_step(sender, "P6", conversation, rows=_product_rows()))
    elif option["id"] == "carrito_listo":
        parts.append(await _finish_cart(sender, conversation))
    elif option["id"] == "p7g_misma":
        parts.append(await _use_saved_shipping(sender, conversation))
    elif option["id"] == "p7g_otra":
        conversation.datos.pop("envio_guardado", None)
        parts.append(await _show_step(sender, "P7", conversation))
    elif destination == "P4C":
        parts.append(await _start_kit(sender, option, conversation))
    elif destination == "T1":
        parts.append(await _handoff(sender, conversation, "solicitud"))
    elif destination == "P11":
        parts.append(await _confirm_order(sender, conversation))
    elif destination == "P11T":
        parts.append(await _confirm_transfer(sender, conversation))
    elif "recomienda" in option:
        parts.append(await _reply_text(sender, recommendation(option["recomienda"])))
        parts.append(await _show_step(sender, "P2", conversation))
    elif "tema_kb" in option:
        answer = await ask_claude(
            f"Elegí el tema «{option['titulo']}» en el menú de dudas. Respóndeme de forma breve.",
            conversation.historial,
        )
        parts.append(await _reply_text(sender, answer) if answer else None)
        parts.append(await _show_step(sender, "P9S", conversation))
    elif destination == "P6":
        parts.append(await _start_order(sender, option, conversation))
    elif destination == "P2F":
        product = get_product(conversation.datos.get("producto", ""))
        parts.append(await _show_step(sender, "P2F", conversation, body=product_card(product)) if product else None)
    elif destination in PASOS_DISPONIBLES:
        parts.append(await _show_step(sender, destination, conversation))
        if destination in _RESPUESTAS_DE_DUDA:
            parts.append(await _show_step(sender, "P9S", conversation))
    else:
        if destination and flow_of(destination) not in _FLUJOS_DE_APOYO:
            conversation.flujo_activo = flow_of(destination)
        conversation.paso = destination
        parts.append(await _reply_text(sender, under_construction(destination or "", option["titulo"])))

    sent = "\n\n".join(p for p in parts if p)
    if sent:
        conversation.add_turn(f"Elegí «{option['titulo']}»", sent)


# ── Pago con link de Bold (P11L → P12 / P12R) ─────────────────────────────────

def _fill(text: str, order: Pedido) -> str:
    return (
        text.replace("[referencia]", order.referencia)
        .replace("[total]", format_price(order.total))
        .replace("[link de pago]", order.link_url or "")
    )


async def _confirm_order(sender: str, conversation: Conversation) -> str | None:
    """«Confirmar pedido»: guarda el pedido y envía el link de pago. Sin Bold, o si falla, pasa a una persona."""
    if not bold.is_enabled():
        return await _handoff(sender, conversation, "pedido")
    # Si el cliente vuelve a tocar «Confirmar pedido», se reenvía el mismo link en vez de crear otro.
    previous = get_order(conversation.datos["pedido_id"]) if conversation.datos.get("pedido_id") else None
    if previous and previous.estado == PENDIENTE and previous.link_url:
        return await _show_step(sender, "P11L", conversation, body=_fill(get_step("P11L")["texto"], previous))
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
    conversation.datos["pedido_id"] = order.id
    archivo.remember_order(
        sender, conversation.nombre, envio, order.referencia,
        "; ".join(describe_item(item).lstrip("• ") for item in items),
    )
    if "Pendiente de pago" not in conversation.etiquetas:
        conversation.etiquetas.append("Pendiente de pago")
    return await _show_step(sender, "P11L", conversation, body=_fill(get_step("P11L")["texto"], order))


def _cancel_unpaid_transfer(order: Pedido | None) -> None:
    """El cliente cambió de medio de pago antes de enviar el comprobante: ese pedido no se cobra."""
    if order and order.medio_pago == TRANSFERENCIA and order.estado == ESPERANDO_COMPROBANTE:
        order.estado = ANULADO
        update_order(order)


async def _confirm_transfer(sender: str, conversation: Conversation) -> str | None:
    """«Transferencia» en el resumen: registra el pedido y da los datos de la cuenta (P11T)."""
    previous = get_order(conversation.datos["pedido_id"]) if conversation.datos.get("pedido_id") else None
    if previous and previous.medio_pago == TRANSFERENCIA and previous.estado in (ESPERANDO_COMPROBANTE, POR_VERIFICAR):
        return await _show_step(sender, "P11T", conversation, body=_fill(get_step("P11T")["texto"], previous))
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
    conversation.datos["pedido_id"] = order.id
    archivo.remember_order(
        sender, conversation.nombre, envio, order.referencia,
        "; ".join(describe_item(item).lstrip("• ") for item in items),
    )
    if "Pendiente de pago" not in conversation.etiquetas:
        conversation.etiquetas.append("Pendiente de pago")
    return await _show_step(sender, "P11T", conversation, body=_fill(get_step("P11T")["texto"], order))


async def handle_media(kind: str, media: dict, sender: str, conversation: Conversation) -> None:
    """Foto o documento del cliente: si tiene un pedido por transferencia pendiente, es el comprobante."""
    if _bot_is_silenced(conversation):
        return
    order = pending_transfer(sender)
    if order is None:
        sent = await _reply_text(sender, get_step("P11A")["texto"])
        if sent:
            conversation.add_turn("[Envié un archivo]", sent)
        return
    mime = (media.get("mime_type") or "").split(";")[0].strip()
    downloaded = await download_media(media["id"]) if mime in transferencias.TIPOS_COMPROBANTE else None
    if downloaded is None or downloaded[1] not in transferencias.TIPOS_COMPROBANTE:
        await _reply_text(sender, get_step("P11P")["texto"])
        return
    path = transferencias.save_receipt(order.referencia, *downloaded)
    order.estado = POR_VERIFICAR
    order.comprobante = str(path)
    update_order(order)
    logger.info("Comprobante recibido para el pedido %s", order.referencia)
    sent = await _reply_text(sender, _fill(get_step("P11C")["texto"], order))
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
        sent = await send_text(order.remitente, _fill(get_step("P11X")["texto"], order))
        aviso = "Le pedimos al cliente que revise y envíe el comprobante otra vez."
        return aviso if sent else (
            f"{aviso} Pero WhatsApp no dejó enviarle el mensaje (pasaron más de 24 h desde su último mensaje): "
            "escríbele desde la app de WhatsApp Business.")
    order.estado = PAGADO
    order.pagado_en = time.time()
    update_order(order)
    sent = await send_text(order.remitente, _fill(get_step("P12")["texto"], order))
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
            await send_text(order.remitente, _fill(get_step("P12R")["texto"], order))
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
    await send_text(order.remitente, _fill(get_step("P12")["texto"], order))
    archivo.remember_payment(order.remitente, order.referencia)
    # Chat completo + ficha: se guarda 1 año y se copia a Google Drive para que el equipo lo lea.
    archivo.save_order_chat(order.id, order.referencia, order.remitente, order.nombre, order_ficha(order))
    await notify_team(
        f"[Saboryarte] 💰 Pedido pagado {order.referencia} — {order.nombre or order.remitente}: despachar",
        order_ficha(order),
    )


# ── Traspaso a una persona (coexistencia con la app de WhatsApp Business) ────

async def _handoff(sender: str, conversation: Conversation, motivo: str) -> str | None:
    """Avisa al cliente, silencia el bot y envía la ficha al equipo."""
    open_now = is_open(_now())
    if motivo == "pedido":
        step = "P11" if open_now else "P11F"
    elif motivo == "no_entiende":
        step = "G4"
    else:
        step = "T1" if open_now else "T2"
    sent = await _show_step(sender, step, conversation)
    if not sent:
        return None
    start_handoff(conversation, motivo)
    logger.info("Traspaso iniciado (motivo: %s)", motivo)
    await notify_team(
        f"[Saboryarte] {MOTIVOS[motivo]} — {conversation.nombre or conversation.remitente}",
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
