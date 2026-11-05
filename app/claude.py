from pathlib import Path

from anthropic import AsyncAnthropic

from .config import settings

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
_BUSINESS_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "negocio.md"

_SYSTEM_PROMPT_TEMPLATE = (
    "Eres el asistente de atención al cliente por WhatsApp de Saboryarte con Sabor. "
    "Respondé siempre en español, de forma breve y amable, basándote únicamente en la "
    "información del negocio que aparece abajo (precios, flujograma de atención, FAQs "
    "y políticas). Si te preguntan algo que no está ahí, decí que no tenés esa "
    "información y que un humano del negocio va a confirmarla; no inventes precios ni datos.\n\n"
    "--- Información del negocio ---\n{business_context}"
)


def _load_business_context() -> str:
    try:
        return _BUSINESS_DATA_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


async def ask_claude(prompt: str) -> str:
    system = _SYSTEM_PROMPT_TEMPLATE.format(business_context=_load_business_context())
    async with _client.messages.stream(
        model="claude-opus-4-8",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = await stream.get_final_message()

    for block in message.content:
        if block.type == "text":
            return block.text
    return ""
