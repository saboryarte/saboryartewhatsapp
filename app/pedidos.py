"""Pedidos confirmados y su estado de pago (SQLite).

El pedido no puede vivir solo en el estado de la conversación: el pago llega por el webhook de Bold, a veces
minutos después, y la conversación caduca a las 24 h. Contiene datos de clientes (data/ no va al repo).
"""
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .flows import format_price
from .orders import describe_item

PENDIENTE = "pendiente_pago"
PAGADO = "pagado"
ANULADO = "anulado"
REVISAR = "revisar"  # el aviso de pago no se pudo confirmar con Bold: lo revisa una persona
# Transferencia bancaria: se espera el comprobante del cliente y luego la verificación del equipo.
ESPERANDO_COMPROBANTE = "esperando_comprobante"
POR_VERIFICAR = "por_verificar"

BOLD, TRANSFERENCIA = "bold", "transferencia"


@dataclass
class Pedido:
    id: int
    remitente: str
    nombre: str | None
    items: list[dict]
    envio: dict
    subtotal: int
    valor_envio: int
    total: int
    estado: str = PENDIENTE
    link_id: str | None = None
    link_url: str | None = None
    payment_id: str | None = None
    creado_en: float = field(default_factory=time.time)
    vence_en: float | None = None
    pagado_en: float | None = None
    medio_pago: str = BOLD
    # Ruta del último comprobante de transferencia recibido (data/pedidos/Comprobantes/).
    comprobante: str | None = None

    @property
    def referencia(self) -> str:
        return f"SAB-{self.id:05d}"


_COLUMNAS = (
    "id", "remitente", "nombre", "items", "envio", "subtotal", "valor_envio", "total", "estado",
    "link_id", "link_url", "payment_id", "creado_en", "vence_en", "pagado_en", "medio_pago", "comprobante",
)
# Columnas agregadas después de crear la tabla: se agregan solas a una base existente.
_COLUMNAS_NUEVAS = {"medio_pago": f"TEXT NOT NULL DEFAULT '{BOLD}'", "comprobante": "TEXT"}


def _connect() -> sqlite3.Connection:
    path = Path(settings.state_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, remitente TEXT NOT NULL, "
        "nombre TEXT, items TEXT NOT NULL, envio TEXT NOT NULL, subtotal INTEGER NOT NULL, "
        "valor_envio INTEGER NOT NULL, total INTEGER NOT NULL, estado TEXT NOT NULL, link_id TEXT UNIQUE, "
        "link_url TEXT, payment_id TEXT, creado_en REAL NOT NULL, vence_en REAL, pagado_en REAL)"
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pedidos)")}
    for column, definition in _COLUMNAS_NUEVAS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE pedidos ADD COLUMN {column} {definition}")
    return conn


def _from_row(row: tuple) -> Pedido:
    data = dict(zip(_COLUMNAS, row))
    data["items"] = json.loads(data["items"])
    data["envio"] = json.loads(data["envio"])
    return Pedido(**data)


def create_order(remitente: str, nombre: str | None, items: list[dict], envio: dict,
                 subtotal: int, valor_envio: int, medio_pago: str = BOLD) -> Pedido:
    now = time.time()
    estado = ESPERANDO_COMPROBANTE if medio_pago == TRANSFERENCIA else PENDIENTE
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO pedidos (remitente, nombre, items, envio, subtotal, valor_envio, total, estado, creado_en, "
            "medio_pago) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (remitente, nombre, json.dumps(items, ensure_ascii=False), json.dumps(envio, ensure_ascii=False),
             subtotal, valor_envio, subtotal + valor_envio, estado, now, medio_pago),
        )
        order_id = cursor.lastrowid
    return get_order(order_id)


def get_order(order_id: int) -> Pedido | None:
    with closing(_connect()) as conn:
        row = conn.execute(f"SELECT {', '.join(_COLUMNAS)} FROM pedidos WHERE id = ?", (order_id,)).fetchone()
    return _from_row(row) if row else None


def find_by_link(link_id: str) -> Pedido | None:
    with closing(_connect()) as conn:
        row = conn.execute(f"SELECT {', '.join(_COLUMNAS)} FROM pedidos WHERE link_id = ?", (link_id,)).fetchone()
    return _from_row(row) if row else None


def find_by_reference(reference: str) -> Pedido | None:
    """Pedido a partir de la referencia que se envía a Bold al crear el link («SAB-00001-<timestamp>»)."""
    match = re.fullmatch(r"SAB-(\d+)-(\d+)", reference)
    order = get_order(int(match.group(1))) if match else None
    # La marca de tiempo debe coincidir: si la numeración se reinicia, un link viejo no se confunde con un pedido nuevo.
    if order and order.link_id and int(order.creado_en) == int(match.group(2)):
        return order
    return None


def update_order(order: Pedido) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE pedidos SET estado = ?, link_id = ?, link_url = ?, payment_id = ?, vence_en = ?, pagado_en = ?, "
            "medio_pago = ?, comprobante = ? WHERE id = ?",
            (order.estado, order.link_id, order.link_url, order.payment_id, order.vence_en, order.pagado_en,
             order.medio_pago, order.comprobante, order.id),
        )


# Un comprobante que llega días después todavía se acepta; más tarde ya no se asocia a un pedido viejo.
_DIAS_PARA_COMPROBANTE = 7


def pending_transfer(remitente: str) -> Pedido | None:
    """Último pedido por transferencia del cliente que espera comprobante (o que ya envió uno y quiere reenviarlo)."""
    since = time.time() - _DIAS_PARA_COMPROBANTE * 24 * 3600
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNAS)} FROM pedidos WHERE remitente = ? AND medio_pago = ? AND estado IN (?, ?) "
            "AND creado_en >= ? ORDER BY id DESC LIMIT 1",
            (remitente, TRANSFERENCIA, ESPERANDO_COMPROBANTE, POR_VERIFICAR, since),
        ).fetchone()
    return _from_row(row) if row else None


def orders_between(start: float, end: float) -> list[Pedido]:
    """Pedidos creados en [start, end), en orden (para el exporte diario)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_COLUMNAS)} FROM pedidos WHERE creado_en >= ? AND creado_en < ? ORDER BY id",
            (start, end),
        ).fetchall()
    return [_from_row(row) for row in rows]


ESTADOS = {
    PENDIENTE: "Esperando pago con Bold",
    PAGADO: "Pagado",
    ANULADO: "Anulado",
    REVISAR: "Por revisar",
    ESPERANDO_COMPROBANTE: "Esperando comprobante",
    POR_VERIFICAR: "Comprobante por verificar",
}
MEDIOS = {BOLD: "Link de Bold", TRANSFERENCIA: "Transferencia"}


def order_ficha(order: Pedido, nota: str | None = None) -> str:
    """Ficha del pedido para el equipo (correo de pago aprobado o por revisar)."""
    envio = order.envio
    lines = [
        f"Pedido {order.referencia} · estado: {ESTADOS.get(order.estado, order.estado)}",
        f"Cliente: {order.nombre or 'sin nombre de perfil'} · WhatsApp {order.remitente}",
        "",
        *(describe_item(item) for item in order.items),
        f"Subtotal: {format_price(order.subtotal)} · Envío: {format_price(order.valor_envio)} · Total: {format_price(order.total)}",
        f"Envío a: {envio['nombre']}, {envio['direccion']}, {envio['ciudad']} · Tel. {envio['telefono']}",
        "",
        f"Medio de pago: {MEDIOS.get(order.medio_pago, order.medio_pago)}",
    ]
    if order.medio_pago == BOLD:
        lines.append(f"Link de pago: {order.link_url or '—'} ({order.link_id or 'sin link'})")
    if order.payment_id:
        lines.append(f"Pago Bold: {order.payment_id}")
    if nota:
        lines += ["", nota]
    return "\n".join(lines)
