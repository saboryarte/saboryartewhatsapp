import random
import re
import unicodedata

# Mensajes de bienvenida con la voz de Saboryarte (docs/flujos/saboryarte.md, §5). Se elige uno al azar
# en cada saludo, a cualquier hora, como texto de la lista del menú (G1). Son de una sola línea, cercanos y
# disponibles; no nombran las opciones (ya están en «Ver opciones», justo debajo) ni hablan del horario: ese
# aviso solo sale cuando el cliente pide una persona (T2) o se le pasa a Juanita.
# "{nombre}" se reemplaza por ", <Nombre>" si conocemos el nombre del perfil, o por "" si no.
WELCOME_MESSAGES: tuple[str, ...] = (
    "¡Hola{nombre}! 🌿 Soy Especia, la asistente virtual de Saboryarte. ¿En qué te puedo ayudar hoy?",
    "¡Hola{nombre}! 🧂 Qué gusto que nos escribas. Soy Especia y estoy lista para ayudarte.",
    "¡Qué más{nombre}! 🌿 Por acá Especia, de Saboryarte. Cuéntame, ¿qué necesitas?",
    "¡Hola{nombre}! ✨ Qué rico tenerte por acá. Soy Especia, ¿en qué te ayudo?",
    "¡Hola{nombre}! 🌿 Gracias por escribirle a Saboryarte. Soy Especia y con gusto te ayudo.",
    "¡Holaaa{nombre}! 🧂 Qué bueno verte por acá. Soy Especia, ¿por dónde empezamos?",
    "¡Hola{nombre}! 🌿 Qué alegría saludarte. Aquí Especia, lista para darle más sabor a tu cocina.",
    "¡Hola{nombre}! ✨ Bienvenida la buena sazón. Soy Especia, de Saboryarte, ¿en qué te ayudo hoy?",
)

# Palabras/frases que, solas o combinadas, cuentan como saludo puro ("hola", "buenas tardes", "hola que tal").
_GREETING_TOKEN = (
    # Letras repetidas permitidas («holaaa», «helloooo», «heyyy»).
    r"(?:h?o+la+|holi+s?|bue+n(?:a+s+|o+s+)?|dias?|tardes?|noches?|he+y+|e+y+|hi+|hel+o+|saludos?|alo+|"
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


def normalize_text(text: str) -> str:
    """Minúsculas, sin tildes, sin signos ni emojis: para comparar lo que escribe el cliente con las opciones."""
    return _normalize(text)


def is_greeting(text: str) -> bool:
    """True si el mensaje es solo un saludo, sin una intención concreta (p. ej. "Hola, quiero pedir" no lo es)."""
    if len(text) > _MAX_GREETING_LENGTH:
        return False
    normalized = _normalize(text)
    return bool(normalized) and bool(_GREETING_RE.match(normalized))


# Pedir una persona en cualquier momento («asesor», «quiero hablar con alguien»…). Ojo: «persona» sola no basta,
# porque aparece en «mezcla personalizada» o «regalo para una persona».
_HUMAN_REQUEST_RE = re.compile(
    r"\basesor(a|es|as)?\b|\bhumano\b|\bagente\b|\bpersona real\b|\balguien del equipo\b"
    r"|\bhablar con (alguien|una persona|un asesor|una asesora|una persona del equipo)\b"
)


def is_human_request(text: str) -> bool:
    return bool(_HUMAN_REQUEST_RE.search(_normalize(text)))


_MENU_REQUESTS = {"menu", "menu principal", "ver menu", "volver al menu", "el menu"}


def is_menu_request(text: str) -> bool:
    """True si el cliente pide el menú («menú», «volver al menú»…)."""
    return _normalize(text) in _MENU_REQUESTS


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
