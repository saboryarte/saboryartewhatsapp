"""Decide qué responder a cada mensaje según el estado de la conversación y los pasos de mensajes.json."""
import logging

from .claude import ask_claude
from .flows import (
    MENU_PRINCIPAL,
    button_rows,
    find_any_option,
    flow_of,
    get_product,
    get_step,
    list_rows,
    product_card,
    recommendation,
    under_construction,
)
from .greetings import is_greeting, is_menu_request, pick_welcome
from .state import Conversation
from .whatsapp import interactive_reply, send_buttons, send_list, send_text

logger = logging.getLogger(__name__)

# Pasos que el bot ya sabe mostrar. Una opción que lleva a cualquier otro responde «en construcción».
PASOS_DISPONIBLES = frozenset({"G1", "P1", "P2", "P2F", "P3", "P4", "P4B", "P5", "P9", "P9B", "P9C", "P9O", "P9S", "O1"})
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
    else:
        reply = await ask_claude(body, conversation.historial)
        if not reply:
            logger.warning("Respuesta de Claude vacía, no se envía mensaje")
            return
        sent = await _reply_text(sender, reply)
        # Después de responder una duda libre, se pregunta si sirvió.
        if sent and conversation.paso == "P9O":
            follow_up = await _show_step(sender, "P9S", conversation)
            sent = "\n\n".join(filter(None, [sent, follow_up]))
    if sent:
        conversation.add_turn(body, sent)


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
