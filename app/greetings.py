import random
import re
import unicodedata

# Mensajes de bienvenida para pruebas. Se elige uno al azar en cada saludo.
# "{nombre}" se reemplaza por ", <Nombre>" si conocemos el nombre del perfil, o por "" si no.
WELCOME_MESSAGES: tuple[str, ...] = (
    "¡Hola{nombre}! 👋 Qué alegría tenerte por aquí. Soy el asistente virtual y estoy listo para ayudarte. "
    "¿Buscas información de productos, quieres hacer un pedido o tienes alguna consulta?",
    "¡Hola{nombre}! 😊 Gracias por escribirnos. Cuéntame, ¿en qué te puedo ayudar hoy? "
    "Puedo orientarte con productos, pedidos o cualquier duda que tengas.",
    "¡Qué gusto saludarte{nombre}! ✨ Estoy aquí para ayudarte con lo que necesites: conocer nuestros productos, "
    "hacer un pedido o resolver una duda. ¿Por dónde empezamos?",
    "¡Hola{nombre}! Te damos la bienvenida 🙌 ¿Qué te gustaría hacer hoy? "
    "Puedo contarte sobre nuestros productos, ayudarte con un pedido o responder tus preguntas.",
    "¡Buenas{nombre}! 👋 Me encanta que nos escribas. Dime qué tienes en mente y con gusto te ayudo: "
    "productos, pedidos o consultas, ¡lo que necesites!",
    "¡Hola{nombre}! 🌟 Aquí tu asistente virtual, con toda la energía para ayudarte. "
    "¿Quieres conocer nuestros productos, hacer un pedido o revisar algo puntual?",
    "¡Hola, hola{nombre}! 😄 Qué bueno que llegaste. ¿En qué te ayudo hoy? "
    "Puedo darte información de productos, apoyarte con tu pedido o resolver cualquier pregunta.",
    "¡Hola{nombre}! Gracias por contactarnos 💬 Con gusto te acompaño en lo que necesites. "
    "¿Te interesa algún producto, quieres pedir algo o necesitas resolver una duda?",
    "¡Hola{nombre}! Es un placer atenderte 😊 Cuéntame qué necesitas y lo resolvemos juntos. "
    "¿Productos, pedidos o una consulta?",
    "¡Hola{nombre}! ☀️ Espero que estés muy bien. Estoy aquí para ayudarte con productos, pedidos "
    "o lo que necesites. ¿Qué te gustaría hacer?",
)

# Palabras/frases que, solas o combinadas, cuentan como saludo puro ("hola", "buenas tardes", "hola que tal").
_GREETING_TOKEN = (
    r"(?:h?ola+|holi+s?|buen(?:as|os)?|dias?|tardes?|noches?|hey+|ey+|hi|hello|saludos?|alo|"
    r"quiubo|quihubo|que\s+(?:tal|mas|hubo|onda|hay)|"
    r"como\s+(?:estas?|estan|vas|van|te\s+va|les\s+va))"
)
_GREETING_RE = re.compile(rf"^(?:{_GREETING_TOKEN}\s*)+$")
_MAX_GREETING_LENGTH = 60

_last_index: int | None = None


def _normalize(text: str) -> str:
    """Minúsculas, sin tildes, sin signos ni emojis y con espacios colapsados."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(text.split())


def is_greeting(text: str) -> bool:
    """True si el mensaje es solo un saludo, sin una intención concreta (p. ej. "Hola, quiero pedir" no lo es)."""
    if len(text) > _MAX_GREETING_LENGTH:
        return False
    normalized = _normalize(text)
    return bool(normalized) and bool(_GREETING_RE.match(normalized))


def _first_name(profile_name: str | None) -> str | None:
    if not profile_name:
        return None
    first = profile_name.strip().split()[0] if profile_name.strip() else ""
    if not first.isalpha() or len(first) > 20:
        return None
    # Respeta mayúsculas mixtas ("JuanPa") y solo corrige nombres todo en mayúsculas o minúsculas.
    return first.capitalize() if first.isupper() or first.islower() else first


def pick_welcome(profile_name: str | None = None) -> str:
    """Devuelve un mensaje de bienvenida aleatorio, evitando repetir el último enviado."""
    global _last_index
    choices = [i for i in range(len(WELCOME_MESSAGES)) if i != _last_index]
    _last_index = random.choice(choices)
    name = _first_name(profile_name)
    return WELCOME_MESSAGES[_last_index].format(nombre=f", {name}" if name else "")
