import random
import re
import unicodedata

# Mensajes de bienvenida con la voz de Saboryarte (docs/flujos/saboryarte.md, §5). Se elige uno al azar
# en cada saludo, a cualquier hora, como texto de la lista del menú (G1). Todos son cercanos, dicen que Especia
# está para ayudar e invitan a abrir «Ver opciones» (el botón de la lista). Ninguno habla del horario: ese aviso
# solo sale cuando el cliente pide una persona (T2) o se le pasa a Juanita.
# "{nombre}" se reemplaza por ", <Nombre>" si conocemos el nombre del perfil, o por "" si no.
WELCOME_MESSAGES: tuple[str, ...] = (
    "¡Hola{nombre}! 🌿 Qué alegría tenerte por acá. Soy Especia, la asistente virtual de Saboryarte, "
    "y estoy lista para ayudarte.\n\n"
    "Toca *Ver opciones* para comprar tus mezclas, pedir regalos corporativos, distribuir Saboryarte o "
    "contarnos que eres proveedor. También puedes escribirme lo que necesites 💛",
    "¡Hola{nombre}! 🧂 Qué gusto que nos escribas. Soy Especia y con mucho cariño te acompaño "
    "en lo que necesites.\n\n"
    "En *Ver opciones* te muestro cómo te puedo ayudar, desde armar tu pedido hasta regalos para tu equipo. "
    "¿Por dónde empezamos?",
    "¡Qué más{nombre}! 🌿 Por acá Especia, de Saboryarte, lista para ayudarte a encontrar tu sabor.\n\n"
    "Escoge en *Ver opciones* lo que buscas, o cuéntame con tus palabras y yo te guío.",
    "¡Hola{nombre}! ✨ Qué rico tenerte por acá. Soy Especia y estoy aquí para ayudarte con nuestras "
    "mezclas, tus pedidos, regalos y más.\n\n"
    "Toca *Ver opciones* y elige la que quieras. Si prefieres, escríbeme y lo resolvemos.",
    "¡Hola{nombre}! 🌿 Gracias por escribirle a Saboryarte. Soy Especia, la asistente virtual, y me "
    "encanta ayudarte.\n\n"
    "Te dejo todo en *Ver opciones*: escoge la que más se parezca a lo que necesitas 💛",
    "¡Holaaa{nombre}! 🧂 Qué bueno verte por acá. Soy Especia y ya estoy lista para atenderte.\n\n"
    "¿Quieres comprar tus mezclas, un regalo corporativo o llevar Saboryarte a tu negocio? "
    "Lo encuentras en *Ver opciones*.",
    "¡Hola{nombre}! 🌿 Qué alegría saludarte. Aquí Especia, con toda la energía para darle más sabor "
    "a tu cocina.\n\n"
    "Cuéntame qué necesitas o toca *Ver opciones* para ver en qué te puedo ayudar.",
    "¡Hola{nombre}! ✨ Bienvenida la buena sazón a esta conversación. Soy Especia, de Saboryarte, y estoy "
    "para ayudarte.\n\n"
    "En *Ver opciones* encuentras todo lo que puedo hacer por ti. ¿Empezamos?",
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
