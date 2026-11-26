"""Traspaso a una persona del equipo (modo coexistencia).

El equipo atiende desde la app de WhatsApp Business. Mientras la conversación está en traspaso el bot no
responde; vuelve cuando la persona envía la frase de cierre o tras HANDOFF_IDLE_HOURS sin mensajes del equipo.
"""
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

from .config import settings
from .flows import format_price
from .orders import describe_item, missing_shipping_fields, shipping_cost, subtotal
from .state import Conversation

# Colombia no tiene horario de verano: UTC−5 todo el año.
COLOMBIA = timezone(timedelta(hours=-5), "COT")
# Atención humana: lunes a sábado, de 8:00 a. m. a 6:00 p. m.
_DIAS_HABILES = range(0, 6)
_APERTURA, _CIERRE = 8, 18

# El equipo la guarda como respuesta rápida en la app: la ve el cliente y le indica al bot que retome.
# Se aceptan también las frases anteriores («…con Chatsabor», «…con nuestro asistente») por si quedaron guardadas así.
CLOSING_PHRASES = ("te dejo de nuevo con especia", "te dejo de nuevo con chatsabor", "te dejo de nuevo con nuestro asistente")

MOTIVOS = {
    "solicitud": "Pidió hablar con una persona",
    "pedido": "Pedido armado: enviar el link de pago y confirmar el despacho",
    "equipo_intervino": "El equipo escribió desde la app",
    "no_entiende": "Especia no entendió dos mensajes seguidos",
    "corporativo": "Regalo corporativo",
    "distribuidor": "Quiere distribuir Saboryarte",
    "proveedor": "Proveedor o quiere ofrecernos algo",
    "queja": "Queja o reclamo",
    "queja_grave": "Reclamo grave: atender ya",
    "alianza": "Alianza, prensa o evento",
    "pago": "La transferencia no llegó: revisar el pago con el cliente",
}
_ETIQUETAS = {
    "pedido": ["Producto", "Pendiente de pago"],
    "corporativo": ["Corporativo"],
    "distribuidor": ["Distribuidor"],
    "proveedor": ["Proveedor"],
    "queja_grave": ["Urgente"],
    "alianza": ["Alianzas"],
    "pago": ["Pagos"],
}
# Regalos corporativos: prioridad alta en la temporada de fin de año.
_MESES_TEMPORADA_REGALOS = (11, 12)
# Despachos: martes y jueves.
_DIAS_DE_DESPACHO = (1, 3)
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def is_open(now: datetime) -> bool:
    local = now.astimezone(COLOMBIA)
    return local.weekday() in _DIAS_HABILES and _APERTURA <= local.hour < _CIERRE


def business_minutes_between(start: datetime, end: datetime) -> float:
    """Minutos de horario de atención entre dos momentos (para medir la espera del cliente)."""
    start, end = start.astimezone(COLOMBIA), end.astimezone(COLOMBIA)
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() in _DIAS_HABILES:
            opening = datetime(day.year, day.month, day.day, _APERTURA, tzinfo=COLOMBIA)
            closing = datetime(day.year, day.month, day.day, _CIERRE, tzinfo=COLOMBIA)
            overlap = (min(end, closing) - max(start, opening)).total_seconds()
            total += max(overlap, 0) / 60
        day += timedelta(days=1)
    return total


def is_closing_message(text: str) -> bool:
    return any(phrase in _normalize(text) for phrase in CLOSING_PHRASES)


def priority(motivo: str, now: datetime) -> str:
    if motivo == "queja_grave":
        return "urgente"
    if motivo == "corporativo" and now.astimezone(COLOMBIA).month in _MESES_TEMPORADA_REGALOS:
        return "alta"
    return "normal"


def dispatch_day(paid: datetime) -> date:
    """Siguiente martes o jueves después del día del pago (el mismo día puede que ya no alcance)."""
    day = paid.astimezone(COLOMBIA).date() + timedelta(days=1)
    while day.weekday() not in _DIAS_DE_DESPACHO:
        day += timedelta(days=1)
    return day


def spanish_date(day: date) -> str:
    return f"{_DIAS[day.weekday()]} {day.day} de {_MESES[day.month - 1]}"


def start_handoff(conversation: Conversation, motivo: str) -> None:
    conversation.en_traspaso = True
    conversation.traspaso = {
        "motivo": motivo,
        "prioridad": priority(motivo, datetime.now(COLOMBIA)),
        "inicio": time.time(),
        "equipo_respondio": motivo == "equipo_intervino",
        "ultima_respuesta_equipo": time.time() if motivo == "equipo_intervino" else None,
        "alerta_enviada": False,
    }
    for etiqueta in _ETIQUETAS.get(motivo, []):
        if etiqueta not in conversation.etiquetas:
            conversation.etiquetas.append(etiqueta)


def register_team_message(conversation: Conversation) -> None:
    conversation.traspaso["equipo_respondio"] = True
    conversation.traspaso["ultima_respuesta_equipo"] = time.time()


def end_handoff(conversation: Conversation) -> None:
    conversation.en_traspaso = False
    conversation.traspaso = None
    conversation.reset_flow()


def team_went_idle(conversation: Conversation, now: float) -> bool:
    """True si el equipo ya respondió y lleva HANDOFF_IDLE_HOURS sin escribir: el bot puede retomar."""
    last = (conversation.traspaso or {}).get("ultima_respuesta_equipo")
    return last is not None and now - last >= settings.handoff_idle_hours * 3600


def needs_alert(conversation: Conversation, now: datetime) -> bool:
    """True si nadie del equipo ha respondido tras HANDOFF_ALERT_MINUTES de horario de atención."""
    traspaso = conversation.traspaso or {}
    if not conversation.en_traspaso or traspaso.get("equipo_respondio") or traspaso.get("alerta_enviada"):
        return False
    started = datetime.fromtimestamp(traspaso["inicio"], tz=COLOMBIA)
    return business_minutes_between(started, now) >= settings.handoff_alert_minutes


def handoff_ficha(conversation: Conversation) -> str:
    """Ficha para la persona del equipo: quién es, qué necesita y qué respondió, para no preguntar de nuevo."""
    traspaso = conversation.traspaso or {}
    started = datetime.fromtimestamp(traspaso.get("inicio", time.time()), tz=COLOMBIA)
    lines = [
        f"Cliente: {conversation.nombre or 'sin nombre de perfil'} · WhatsApp {conversation.remitente}",
        f"Motivo: {MOTIVOS.get(traspaso.get('motivo'), traspaso.get('motivo', '—'))}",
        f"Prioridad: {traspaso.get('prioridad', 'normal')}",
        f"Hora del traspaso: {started:%Y-%m-%d %H:%M} (hora Colombia)",
    ]
    if conversation.etiquetas:
        lines.append(f"Etiquetas: {', '.join(conversation.etiquetas)}")
    if conversation.ruta:
        lines.append(f"Ruta: {' → '.join(conversation.ruta)}")
    if conversation.datos.get("detalle"):
        lines.append(f"Lo que contó el cliente: {conversation.datos['detalle']}")

    datos = conversation.datos
    if datos.get("pedido"):
        lines += ["", "Pedido:", *(describe_item(item) for item in datos["pedido"])]
        sub = subtotal(datos["pedido"])
        envio = datos.get("envio", {})
        if not missing_shipping_fields(envio):
            costo = shipping_cost(envio["ciudad"])
            lines += [
                f"Subtotal: {format_price(sub)} · Envío: {format_price(costo)} · Total: {format_price(sub + costo)}",
                f"Envío a: {envio['nombre']}, {envio['direccion']}, {envio['ciudad']} · Tel. {envio['telefono']}",
            ]
        else:
            lines.append(f"Subtotal: {format_price(sub)} (faltan datos de envío)")

    recent = conversation.historial[-6:]
    if recent:
        lines += ["", "Últimos mensajes:"]
        lines += [f"{'Cliente' if m['role'] == 'user' else 'Bot'}: {m['content']}" for m in recent]
    return "\n".join(lines)
