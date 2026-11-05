"""Archivo de chats, memoria de clientes y chat de cada pedido pagado (SQLite + archivos legibles).

La conversación activa (paso, carrito, contexto de Claude) sigue reiniciándose a las 24 h (state.py). Lo que vive
más tiempo está aquí, con retención fija (Ley 1581 de 2012: guardar solo lo necesario y por un tiempo definido):

- chat:           todos los mensajes (cliente, Chatsabor y equipo) ........ 60 días
- clientes:       nombre, último envío y último pedido, para reconocer al cliente que vuelve
                  ........ 60 días sin actividad, o 1 año desde su último pedido pagado
- chats_pedidos:  chat completo + ficha de cada pedido pagado ............. 1 año
                  También se escribe como texto en data/pedidos/ para copiarlo a Google Drive (scripts/respaldo.sh).

Contiene datos personales de clientes: data/ no va al repo.
"""
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .config import settings
from .handoff import COLOMBIA

logger = logging.getLogger(__name__)

DIA = 24 * 3600
CHAT_TTL_SECONDS = 60 * DIA
CLIENTE_TTL_SECONDS = 60 * DIA
PEDIDO_TTL_SECONDS = 365 * DIA
# Una pausa de más de 24 h separa una conversación de la siguiente (como la ventana de WhatsApp).
_PAUSA_ENTRE_CONVERSACIONES = DIA

CLIENTE, CHATSABOR, EQUIPO = "cliente", "chatsabor", "equipo"


def _connect() -> sqlite3.Connection:
    path = Path(settings.state_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, remitente TEXT NOT NULL, "
        "quien TEXT NOT NULL, texto TEXT NOT NULL, creado_en REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS chat_por_remitente ON chat (remitente, creado_en)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS clientes (remitente TEXT PRIMARY KEY, nombre TEXT, envio TEXT, "
        "ultimo_pedido TEXT, pagado_en REAL, actualizado_en REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chats_pedidos (pedido_id INTEGER PRIMARY KEY, referencia TEXT NOT NULL, "
        "remitente TEXT NOT NULL, texto TEXT NOT NULL, archivo TEXT, creado_en REAL NOT NULL)"
    )
    return conn


def pedidos_dir() -> Path:
    return Path(settings.state_db_path).parent / "pedidos"


# ── Archivo de chats ──────────────────────────────────────────────────────────

def record(remitente: str, quien: str, texto: str) -> None:
    """Guarda un mensaje en el archivo. Nunca interrumpe la atención: si falla, solo queda en el log."""
    if not texto:
        return
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO chat (remitente, quien, texto, creado_en) VALUES (?, ?, ?, ?)",
                (remitente, quien, texto, time.time()),
            )
    except sqlite3.Error:
        logger.exception("No se pudo guardar un mensaje en el archivo de chats")


def outgoing_text(message: dict) -> str:
    """Texto legible de un mensaje que envía Chatsabor (texto, lista, botones o formulario)."""
    if message.get("type") == "text":
        return message["text"]["body"]
    interactive = message.get("interactive") or {}
    body = (interactive.get("body") or {}).get("text", "")
    action = interactive.get("action") or {}
    if interactive.get("type") == "list":
        titles = [row["title"] for section in action.get("sections", []) for row in section.get("rows", [])]
        return f"{body}\n[Lista «{action.get('button', '')}»: {' / '.join(titles)}]"
    if interactive.get("type") == "button":
        return f"{body}\n[Botones: {' / '.join(b['reply']['title'] for b in action.get('buttons', []))}]"
    if interactive.get("type") == "flow":
        return f"{body}\n[Formulario: {(action.get('parameters') or {}).get('flow_cta', '')}]"
    return body


def last_session(remitente: str, until: float) -> list[tuple[str, str, float]]:
    """Mensajes de la última conversación hasta «until»: se retrocede hasta una pausa de más de 24 h."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT quien, texto, creado_en FROM chat WHERE remitente = ? AND creado_en <= ? ORDER BY creado_en DESC, id DESC",
            (remitente, until),
        ).fetchall()
    session = []
    for row in rows:
        if session and session[-1][2] - row[2] > _PAUSA_ENTRE_CONVERSACIONES:
            break
        session.append(row)
    return list(reversed(session))


# ── Memoria del cliente que vuelve ────────────────────────────────────────────

def remember_order(remitente: str, nombre: str | None, envio: dict, referencia: str, resumen: str) -> None:
    """Al confirmar un pedido: guarda los datos de envío para ofrecerlos la próxima vez."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO clientes (remitente, nombre, envio, ultimo_pedido, actualizado_en) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(remitente) DO UPDATE SET nombre = COALESCE(excluded.nombre, clientes.nombre), "
            "envio = excluded.envio, ultimo_pedido = excluded.ultimo_pedido, actualizado_en = excluded.actualizado_en",
            (remitente, nombre, json.dumps(envio, ensure_ascii=False),
             json.dumps({"referencia": referencia, "resumen": resumen, "estado": "pendiente de pago"}, ensure_ascii=False),
             time.time()),
        )


def remember_payment(remitente: str, referencia: str) -> None:
    now = time.time()
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT ultimo_pedido FROM clientes WHERE remitente = ?", (remitente,)).fetchone()
        if row is None:
            return
        pedido = json.loads(row[0] or "{}")
        if pedido.get("referencia") == referencia:
            pedido["estado"] = "pagado"
            pedido["pagado"] = datetime.fromtimestamp(now, tz=COLOMBIA).strftime("%Y-%m-%d")
        conn.execute(
            "UPDATE clientes SET ultimo_pedido = ?, pagado_en = ?, actualizado_en = ? WHERE remitente = ?",
            (json.dumps(pedido, ensure_ascii=False), now, now, remitente),
        )


def touch_client(remitente: str) -> None:
    """El cliente volvió a escribir: su memoria vale 60 días más desde hoy."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE clientes SET actualizado_en = ? WHERE remitente = ?", (time.time(), remitente))


def saved_shipping(remitente: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT envio FROM clientes WHERE remitente = ?", (remitente,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def client_note(remitente: str | None) -> str | None:
    """Nota para Claude sobre el cliente que vuelve. Sin dirección ni teléfono: no los necesita para responder."""
    if not remitente:
        return None
    with closing(_connect()) as conn:
        row = conn.execute("SELECT ultimo_pedido, envio FROM clientes WHERE remitente = ?", (remitente,)).fetchone()
    if not row or not row[0]:
        return None
    pedido = json.loads(row[0])
    ciudad = (json.loads(row[1]) if row[1] else {}).get("ciudad")
    fecha = f" el {pedido['pagado']}" if pedido.get("pagado") else ""
    destino = f", enviado a {ciudad}" if ciudad else ""
    return (
        f"Este cliente ya compró antes. Su último pedido fue {pedido['referencia']} ({pedido['estado']}{fecha}{destino}): "
        f"{pedido['resumen']}. Si viene al caso, salúdalo como cliente que vuelve. No conoces el estado del despacho."
    )


# ── Chat de cada pedido pagado ────────────────────────────────────────────────

def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE).strip()[:40] or "cliente"


def save_order_chat(pedido_id: int, referencia: str, remitente: str, nombre: str | None, ficha: str) -> Path | None:
    """Guarda la ficha y el chat completo del pedido pagado (1 año) y lo escribe como .txt para el equipo."""
    now = time.time()
    lines = [ficha, "", "─" * 40, "Conversación (hora de Colombia)", "─" * 40]
    for quien, texto, creado_en in last_session(remitente, now):
        hora = datetime.fromtimestamp(creado_en, tz=COLOMBIA).strftime("%Y-%m-%d %H:%M")
        etiqueta = {"cliente": nombre or "Cliente", "chatsabor": "Chatsabor", "equipo": "Equipo"}.get(quien, quien)
        lines.append(f"[{hora}] {etiqueta}: {texto}")
    text = "\n".join(lines)
    fecha = datetime.fromtimestamp(now, tz=COLOMBIA).strftime("%Y-%m-%d")
    path = pedidos_dir() / f"{fecha} {referencia} {_safe_filename(nombre or '')}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError:
        logger.exception("No se pudo escribir el archivo del pedido %s", referencia)
        path = None
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO chats_pedidos (pedido_id, referencia, remitente, texto, archivo, creado_en) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pedido_id, referencia, remitente, text, str(path) if path else None, now),
        )
    return path


# ── Retención ─────────────────────────────────────────────────────────────────

def purge(now: float | None = None) -> dict[str, int]:
    """Borra lo que ya cumplió su plazo. Se ejecuta una vez al día desde main.py."""
    now = now or time.time()
    with closing(_connect()) as conn, conn:
        chat = conn.execute("DELETE FROM chat WHERE creado_en < ?", (now - CHAT_TTL_SECONDS,)).rowcount
        clientes = conn.execute(
            "DELETE FROM clientes WHERE actualizado_en < ? AND (pagado_en IS NULL OR pagado_en < ?)",
            (now - CLIENTE_TTL_SECONDS, now - PEDIDO_TTL_SECONDS),
        ).rowcount
        viejos = conn.execute(
            "SELECT archivo FROM chats_pedidos WHERE creado_en < ?", (now - PEDIDO_TTL_SECONDS,)
        ).fetchall()
        pedidos = conn.execute("DELETE FROM chats_pedidos WHERE creado_en < ?", (now - PEDIDO_TTL_SECONDS,)).rowcount
    for (archivo,) in viejos:
        if archivo:
            Path(archivo).unlink(missing_ok=True)
    if chat or clientes or pedidos:
        logger.info("Retención: %d mensajes, %d clientes y %d chats de pedidos borrados", chat, clientes, pedidos)
    return {"chat": chat, "clientes": clientes, "chats_pedidos": pedidos}
