"""Estado de conversación por remitente y registro de mensajes procesados (SQLite)."""
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import settings

# Una conversación sin actividad durante 24 h empieza de cero (coincide con la ventana de WhatsApp).
CONVERSATION_TTL_SECONDS = 24 * 3600
# Los ids procesados se guardan una semana: suficiente para cubrir los reintentos de Meta.
PROCESSED_TTL_SECONDS = 7 * 24 * 3600
# Máximo de mensajes (cliente + asistente) que se guardan como contexto para Claude.
MAX_HISTORIAL = 20
# Pasos recientes que se guardan para la ficha de traspaso.
MAX_RUTA = 20


@dataclass
class Conversation:
    remitente: str
    nombre: str | None = None
    flujo_activo: str | None = None
    paso: str | None = None
    datos: dict = field(default_factory=dict)
    historial: list[dict] = field(default_factory=list)
    intentos_fallidos: int = 0
    en_traspaso: bool = False
    etiquetas: list[str] = field(default_factory=list)
    # Datos del traspaso en curso: motivo, prioridad, inicio, respuestas del equipo, alerta enviada.
    traspaso: dict | None = None
    ruta: list[str] = field(default_factory=list)
    # Lo último que se le mostró al cliente para responder: paso, tipo (lista, botones, formulario, texto…), texto,
    # botón de la lista y filas/botones. Sirve para volver a mostrar las opciones y reconocer lo que escribe.
    opciones: dict | None = None
    actualizado_en: float = 0.0

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        """Agrega un intercambio completo; el historial siempre empieza con un mensaje del cliente."""
        # Lista nueva en lugar de modificarla: quien ya recibió el historial (p. ej. ask_claude) no ve el cambio.
        self.historial = [
            *self.historial,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ][-MAX_HISTORIAL:]

    def add_step(self, step_id: str) -> None:
        self.ruta = [*self.ruta, step_id][-MAX_RUTA:]

    def reset_flow(self) -> None:
        self.flujo_activo = None
        self.paso = None
        self.datos = {}
        self.intentos_fallidos = 0


def _connect() -> sqlite3.Connection:
    path = Path(settings.state_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversaciones "
        "(remitente TEXT PRIMARY KEY, estado TEXT NOT NULL, actualizado_en REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mensajes_procesados (id TEXT PRIMARY KEY, recibido_en REAL NOT NULL)"
    )
    return conn


def mark_processed(message_id: str) -> bool:
    """Registra el id del mensaje. Devuelve False si ya se había procesado (reintento de Meta)."""
    now = time.time()
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM mensajes_procesados WHERE recibido_en < ?", (now - PROCESSED_TTL_SECONDS,))
        cursor = conn.execute(
            "INSERT OR IGNORE INTO mensajes_procesados (id, recibido_en) VALUES (?, ?)", (message_id, now)
        )
        return cursor.rowcount == 1


def load_conversation(remitente: str) -> Conversation:
    """Devuelve el estado guardado, o uno nuevo si no existe o caducó."""
    now = time.time()
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM conversaciones WHERE actualizado_en < ?", (now - CONVERSATION_TTL_SECONDS,))
        row = conn.execute("SELECT estado FROM conversaciones WHERE remitente = ?", (remitente,)).fetchone()
    if row is None:
        return Conversation(remitente=remitente)
    return Conversation(**json.loads(row[0]))


def save_conversation(conversation: Conversation) -> None:
    conversation.actualizado_en = time.time()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO conversaciones (remitente, estado, actualizado_en) VALUES (?, ?, ?) "
            "ON CONFLICT(remitente) DO UPDATE SET estado = excluded.estado, actualizado_en = excluded.actualizado_en",
            (conversation.remitente, json.dumps(asdict(conversation), ensure_ascii=False), conversation.actualizado_en),
        )


def list_handoffs() -> list[Conversation]:
    """Conversaciones vigentes que están en traspaso (para la alerta de «nadie respondió»)."""
    now = time.time()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT estado FROM conversaciones WHERE actualizado_en >= ?", (now - CONVERSATION_TTL_SECONDS,)
        ).fetchall()
    conversations = [Conversation(**json.loads(row[0])) for row in rows]
    return [c for c in conversations if c.en_traspaso]
