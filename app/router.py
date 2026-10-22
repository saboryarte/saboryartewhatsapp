"""Decide qué responder a cada mensaje según el estado de la conversación y los pasos de mensajes.json."""
import logging

from .claude import ask_claude, extract_order, extract_shipping
from .flows import (
    MENU_PRINCIPAL,
    button_rows,
    find_any_option,
    flow_of,
    get_product,
    get_step,
    list_rows,
    mix_ids,
    product_card,
    recommendation,
    under_construction,
)
from .greetings import is_greeting, is_menu_request, pick_welcome
from .orders import ask_missing_shipping, missing_shipping_fields, order_summary
from .state import Conversation
from .whatsapp import interactive_reply, send_buttons, send_list, send_text

logger = logging.getLogger(__name__)

# Pasos que el bot ya sabe mostrar. Una opción que lleva a cualquier otro responde «en construcción».
PASOS_DISPONIBLES = frozenset({
    "G1", "P1", "P2", "P2F", "P3", "P4", "P4B", "P5", "P6", "P7", "P8", "P9", "P9B", "P9C", "P9O", "P9S", "O1",
})
# Respuestas a una duda: después se pregunta «¿Te sirvió?» (P9S).
_RESPUESTAS_DE_DUDA = frozenset({"P9B", "P9C", "O1"})
# Pasos de otras rutas que se usan como respuesta dentro de la ruta actual: no cambian el flujo activo.
_FLUJOS_DE_APOYO = frozenset({"entrada", "otras"})


async def _show_step(sender: str, step_id: str, conversation: Conversation, body: str | None = None) -> str | None:
    """Envía un paso según su tipo y actualiza el estado. Devuelve el texto enviado, o None si Meta lo rechazó."""
    step = get_step(step_id)
    text = body or step["texto"]
    if step["tipo"] == "lista":
        sent = await send_list(sender, text, step["boton_lista"], list_rows(step_id))
    elif step["tipo"] == "botones":
        sent = await send_buttons(sender, text, button_rows(step_id))
    else:
        sent = await send_text(sender, text)
    if not sent:
        return None
    if step_id == MENU_PRINCIPAL:
        conversation.reset_flow()
    elif flow_of(step_id) not in _FLUJOS_DE_APOYO:
        conversation.flujo_activo = flow_of(step_id)
    conversation.paso = step_id
    return text


async def _reply_text(sender: str, text: str) -> str | None:
    return text if await send_text(sender, text) else None


async def handle_text(body: str, sender: str, profile_name: str | None, conversation: Conversation) -> None:
    if is_menu_request(body):
        logger.info("Menú solicitado, se reinicia el flujo")
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation)
    # Un saludo a mitad de un flujo no lo reinicia: se responde en contexto.
    elif conversation.flujo_activo is None and is_greeting(body):
        logger.info("Saludo detectado, se envía el menú con bienvenida aleatoria")
        sent = await _show_step(sender, MENU_PRINCIPAL, conversation, body=pick_welcome(profile_name))
    elif conversation.paso == "P6":
        sent = await _take_order_items(body, sender, conversation)
    elif conversation.paso == "P7":
        sent = await _take_shipping(body, sender, conversation)
    else:
        sent = await _answer_with_claude(body, sender, conversation)
    if sent:
        conversation.add_turn(body, sent)


async def _answer_with_claude(body: str, sender: str, conversation: Conversation) -> str | None:
    reply = await ask_claude(body, conversation.historial)
    if not reply:
        logger.warning("Respuesta de Claude vacía, no se envía mensaje")
        return None
    sent = await _reply_text(sender, reply)
    # Después de responder una duda libre, se pregunta si sirvió.
    if sent and conversation.paso == "P9O":
        follow_up = await _show_step(sender, "P9S", conversation)
        sent = "\n\n".join(filter(None, [sent, follow_up]))
    return sent


# ── Pedido: P6 (qué y cuánto) → P7 (envío) → P8 (resumen) ─────────────────────

_REINTENTO_TECNICO = "Uy, tuve un problema técnico leyendo tu mensaje 🙈 ¿Me lo repites, por favor?"
_DISTRIBUIR = (
    "¡Qué bueno que quieras llevar Saboryarte a tu negocio! 🌿 Para distribuidores tenemos una ruta especial "
    "que todavía está en construcción.\n\nMientras tanto, si es para ti, cuéntame qué mezclas quieres y cuántas."
)


def _order_prompt(conversation: Conversation) -> str:
    """Pregunta de P6 según cómo se llegó: con un kit, con un producto elegido o desde cero."""
    kit_size = conversation.datos.get("kit")
    if kit_size:
        return (
            f"¡Arma tu kit de {kit_size} mezclas! 🎁 ¿Cuáles quieres? Escríbelas en un mensaje "
            "(por ejemplo: «oro, sol y chilemon»)."
        )
    product = get_product(conversation.datos.get("producto", ""))
    if product:
        return (
            f"¡Listo! Armemos tu pedido 🙌 ¿Cuántas unidades de *{product['nombre']}* quieres? "
            "Si quieres agregar otras mezclas, escríbelas también."
        )
    return get_step("P6")["texto"]


async def _start_order(sender: str, option: dict, conversation: Conversation) -> str | None:
    conversation.datos.pop("pedido", None)
    conversation.datos.pop("kit", None)
    if "kit" in option:
        conversation.datos["kit"] = option["kit"]
        conversation.datos.pop("producto", None)
    return await _show_step(sender, "P6", conversation, body=_order_prompt(conversation))


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
    selected = get_product(conversation.datos.get("producto", ""))
    extraction = await extract_order(body, selected["nombre"] if selected else None, conversation.datos.get("kit"))
    if extraction is None:
        return await _reply_text(sender, _REINTENTO_TECNICO)
    if extraction.quiere_distribuir:
        return await _reply_text(sender, _DISTRIBUIR)
    if extraction.pregunta_o_comentario and not extraction.lineas:
        # Una pregunta a mitad del pedido: se responde y se sigue esperando los productos.
        return await _answer_with_claude(body, sender, conversation)
    items, retry = _order_items(extraction, conversation)
    if items is None:
        return await _reply_text(sender, retry)
    conversation.datos["pedido"] = items
    logger.info("Pedido capturado: %d ítem(s)", len(items))
    # Si ya tenemos los datos de envío (p. ej. el cliente tocó «Cambiar algo»), se va directo al resumen.
    if not missing_shipping_fields(conversation.datos.get("envio", {})):
        return await _show_summary(sender, conversation)
    return await _show_step(sender, "P7", conversation)


async def _take_shipping(body: str, sender: str, conversation: Conversation) -> str | None:
    extraction = await extract_shipping(body)
    if extraction is None:
        return await _reply_text(sender, _REINTENTO_TECNICO)
    found = {field: value.strip() for field, value in extraction.model_dump(exclude={"pregunta_o_comentario"}).items() if value and value.strip()}
    if extraction.pregunta_o_comentario and not found:
        return await _answer_with_claude(body, sender, conversation)
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
    destination = option.get("va_a")
    logger.info("Opción elegida: %s (%s → %s)", option["id"], step_id, destination)
    if "producto" in option:
        conversation.datos["producto"] = option["producto"]

    parts: list[str | None] = []
    if "recomienda" in option:
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
