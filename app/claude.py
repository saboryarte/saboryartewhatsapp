import logging

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from .config import settings
from .flows import orderable_products
from .prompt import system_prompt
from .usage import record_claude

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

# Sonnet 5.5 es el modelo más alto que usa el bot (decisión de costo del 21 oct 2026; ver evals/).
# Sin «fallbacks» del servidor: podrían enrutar a un modelo más caro.
_RESPONSE_MODEL = "claude-sonnet-5-5"


async def ask_claude(prompt: str, history: list[dict] | None = None) -> str:
    """history: intercambios previos de la conversación, alternando user/assistant y empezando por user."""
    async with _client.messages.stream(
        model=_RESPONSE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        # Chat de atención: esfuerzo bajo. Aprobó 45/48 en evals/comparar_modelos.py.
        output_config={"effort": "low"},
        # El system prompt (voz de marca + base de conocimiento) es igual en cada llamada: se cachea.
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=[*(history or []), {"role": "user", "content": prompt}],
    ) as stream:
        message = await stream.get_final_message()
    # Se registra antes de revisar el rechazo: una respuesta declinada también se cobra.
    record_claude(_RESPONSE_MODEL, message.usage)

    if message.stop_reason == "refusal":
        logger.warning("Respuesta declinada por el modelo")
        return ""
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


# ── Extracción estructurada (pedido y datos de envío) ─────────────────────────
# Claude solo interpreta el texto del cliente; el código valida contra el catálogo y hace las cuentas.

# Extracción simple y con esquema: Haiku 5.5 basta y cuesta una fracción.
_EXTRACTION_MODEL = "claude-haiku-5-5"


class LineaPedido(BaseModel):
    producto: str
    cantidad: int


class PedidoExtraido(BaseModel):
    lineas: list[LineaPedido]
    no_reconocidos: list[str]
    quiere_distribuir: bool
    pregunta_o_comentario: bool


class EnvioExtraido(BaseModel):
    nombre: str | None
    direccion: str | None
    ciudad: str | None
    telefono: str | None
    pregunta_o_comentario: bool


def _order_instructions() -> str:
    catalogo = "\n".join(f"- {p['id']}: {p['nombre']} ({p['presentacion']})" for p in orderable_products())
    return f"""Extraes los productos y cantidades de un mensaje de WhatsApp de un cliente de Saboryarte.

Catálogo (id: nombre):
{catalogo}

Reglas:
- En «lineas» usa solo ids del catálogo. Nombres cortos o informales cuentan: «oro» = sal_de_oro, «chilemon» o «la de limón» = sal_chilemon, «la ahumada» = tierra_de_fuego.
- Convierte cantidades escritas en palabras a números. Si no dice cantidad, usa 1.
- Si el cliente ya había elegido un producto y solo responde con una cantidad, aplícala a ese producto.
- «no_reconocidos»: productos que menciona y no están en el catálogo (por ejemplo, «sal negra» o «granel»).
- «quiere_distribuir»: true si habla de revender, distribuir, comprar a granel o para su negocio.
- «pregunta_o_comentario»: true si el mensaje no trae productos para el pedido (una pregunta u otro tema)."""


_SHIPPING_INSTRUCTIONS = """Extraes los datos de envío de un mensaje de WhatsApp de un cliente en Colombia.

Reglas:
- «nombre»: nombre completo de quien recibe. «direccion»: dirección con barrio o referencias si las da. «ciudad»: ciudad o municipio. «telefono»: número de contacto, solo dígitos y «+».
- Usa null para los datos que el mensaje no trae. No inventes ni completes datos.
- «pregunta_o_comentario»: true si el mensaje no trae datos de envío (una pregunta u otro tema)."""


async def _extract(instructions: str, content: str, output_format: type[BaseModel]) -> BaseModel | None:
    """Devuelve la extracción validada, o None si la API falla o el modelo declina."""
    try:
        response = await _client.messages.parse(
            model=_EXTRACTION_MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            system=instructions,
            messages=[{"role": "user", "content": content}],
            output_format=output_format,
        )
    except anthropic.APIConnectionError:
        logger.error("Extracción: no se pudo conectar con la API de Anthropic")
        return None
    except anthropic.APIStatusError as e:
        logger.error("Extracción: error de la API de Anthropic (HTTP %s)", e.status_code)
        return None
    record_claude(_EXTRACTION_MODEL, response.usage)
    if response.stop_reason == "refusal":
        logger.warning("Extracción declinada por el modelo")
        return None
    return response.parsed_output


async def extract_order(text: str, selected_product: str | None = None, kit_size: int | None = None) -> PedidoExtraido | None:
    context = []
    if selected_product:
        context.append(f"El cliente ya había elegido: {selected_product}.")
    if kit_size:
        context.append(f"El cliente está armando un kit de {kit_size} mezclas; el mensaje dice cuáles quiere.")
    content = "\n".join([*context, f"Mensaje del cliente: «{text}»"])
    return await _extract(_order_instructions(), content, PedidoExtraido)


async def extract_shipping(text: str) -> EnvioExtraido | None:
    return await _extract(_SHIPPING_INSTRUCTIONS, f"Mensaje del cliente: «{text}»", EnvioExtraido)
