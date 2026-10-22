"""Registro del costo real: tokens de Claude y mensajes de WhatsApp enviados, agrupados por conversación (SQLite).

Reporte:  .venv/bin/python -m app.usage            (últimos 30 días)
          .venv/bin/python -m app.usage --dias 7
"""
import argparse
import logging
import sqlite3
import time
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from .config import settings
from .handoff import COLOMBIA
from .state import CONVERSATION_TTL_SECONDS

logger = logging.getLogger(__name__)

# US$ por millón de tokens (tabla de precios del 2026-10-06). Escritura de caché (TTL de 5 min) = 1,25 × entrada.
PRECIOS_CLAUDE = {
    "claude-sonnet-5-5": {"entrada": 2.00, "salida": 10.00, "cache_lectura": 0.20},
    "claude-haiku-5-5": {"entrada": 0.10, "salida": 0.50, "cache_lectura": 0.01},
    "claude-opus-5-5": {"entrada": 4.00, "salida": 20.00, "cache_lectura": 0.20},
}
# Mensajes de servicio de WhatsApp (desde el 1 oct 2026): 1.000 gratis al mes por número y luego la tarifa del país.
# Tarifa de Colombia en US$ (oct 2026, igual a la de utilidad); confírmala en la tabla de Meta si cambia.
TARIFA_SERVICIO_USD = 0.0008
SERVICIO_GRATIS_MES = 1000
# El registro se guarda 90 días: alcanza para comparar meses. Contiene números de clientes (data/ no va al repo).
USO_TTL_SECONDS = 90 * 24 * 3600

# Cliente de la conversación que se está atendiendo. Lo fija main.py; las llamadas a Claude lo leen para registrarse.
remitente_actual: ContextVar[str | None] = ContextVar("remitente_actual", default=None)


def claude_cost(model: str, usage) -> float:
    """Costo en US$ de una respuesta, a partir de su `usage` (la salida incluye los tokens de razonamiento)."""
    p = PRECIOS_CLAUDE[model]
    return (
        usage.input_tokens * p["entrada"]
        + (usage.cache_creation_input_tokens or 0) * p["entrada"] * 1.25
        + (usage.cache_read_input_tokens or 0) * p["cache_lectura"]
        + usage.output_tokens * p["salida"]
    ) / 1_000_000


def _connect() -> sqlite3.Connection:
    path = Path(settings.state_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS uso (remitente TEXT, tipo TEXT NOT NULL, detalle TEXT, "
        "entrada INTEGER NOT NULL DEFAULT 0, cache_escritura INTEGER NOT NULL DEFAULT 0, "
        "cache_lectura INTEGER NOT NULL DEFAULT 0, salida INTEGER NOT NULL DEFAULT 0, "
        "costo_usd REAL NOT NULL DEFAULT 0, creado_en REAL NOT NULL)"
    )
    return conn


def _insert(row: dict) -> None:
    """Guarda un evento de uso. Si falla, solo se registra en el log: medir nunca debe frenar una respuesta."""
    now = time.time()
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("DELETE FROM uso WHERE creado_en < ?", (now - USO_TTL_SECONDS,))
            conn.execute(
                f"INSERT INTO uso ({', '.join(row)}, creado_en) VALUES ({', '.join('?' * len(row))}, ?)",
                (*row.values(), now),
            )
    except sqlite3.Error:
        logger.exception("No se pudo registrar el uso (%s)", row.get("tipo"))


def record_claude(model: str, usage) -> None:
    _insert({
        "remitente": remitente_actual.get(),
        "tipo": "claude",
        "detalle": model,
        "entrada": usage.input_tokens,
        "cache_escritura": usage.cache_creation_input_tokens or 0,
        "cache_lectura": usage.cache_read_input_tokens or 0,
        "salida": usage.output_tokens,
        "costo_usd": claude_cost(model, usage),
    })


def record_whatsapp(to: str, message_type: str) -> None:
    """Un mensaje que Meta aceptó. Su costo depende del total del mes, así que se calcula en el reporte."""
    _insert({"remitente": to, "tipo": "whatsapp", "detalle": message_type})


# ── Reporte ───────────────────────────────────────────────────────────────────

def conversation_costs(desde: float) -> list[dict]:
    """Uso por conversación desde `desde`. Una pausa de más de 24 h con el mismo cliente abre otra conversación."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT remitente, tipo, entrada, cache_escritura, cache_lectura, salida, costo_usd, creado_en "
            "FROM uso WHERE creado_en >= ? ORDER BY remitente, creado_en",
            (desde,),
        ).fetchall()
    conversations: list[dict] = []
    for remitente, tipo, entrada, cache_escritura, cache_lectura, salida, costo, creado_en in rows:
        current = conversations[-1] if conversations else None
        if current is None or current["remitente"] != remitente or creado_en - current["fin"] > CONVERSATION_TTL_SECONDS:
            current = {
                "remitente": remitente, "inicio": creado_en, "fin": creado_en, "llamadas_claude": 0,
                "entrada": 0, "cache_escritura": 0, "cache_lectura": 0, "salida": 0, "costo_claude": 0.0, "mensajes": 0,
            }
            conversations.append(current)
        current["fin"] = creado_en
        if tipo == "claude":
            current["llamadas_claude"] += 1
            current["entrada"] += entrada
            current["cache_escritura"] += cache_escritura
            current["cache_lectura"] += cache_lectura
            current["salida"] += salida
            current["costo_claude"] += costo
        else:
            current["mensajes"] += 1
    return sorted(conversations, key=lambda c: c["inicio"])


def whatsapp_costs_by_month(desde: float) -> list[dict]:
    """Mensajes enviados por mes (hora de Colombia) y costo estimado tras los gratuitos del mes.

    Cuenta los meses completos que toca el periodo, porque los gratuitos se aplican al total del mes.
    Solo incluye lo que envía el bot, no lo que el equipo escribe desde la app.
    """
    inicio = datetime.fromtimestamp(desde, tz=COLOMBIA).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT creado_en FROM uso WHERE tipo = 'whatsapp' AND creado_en >= ?", (inicio.timestamp(),)
        ).fetchall()
    counts: dict[str, int] = {}
    for (creado_en,) in rows:
        mes = datetime.fromtimestamp(creado_en, tz=COLOMBIA).strftime("%Y-%m")
        counts[mes] = counts.get(mes, 0) + 1
    months = []
    for mes, enviados in sorted(counts.items()):
        cobrables = max(0, enviados - SERVICIO_GRATIS_MES)
        months.append({"mes": mes, "enviados": enviados, "cobrables": cobrables, "costo": cobrables * TARIFA_SERVICIO_USD})
    return months


def _usd(value: float, decimals: int = 4) -> str:
    return f"US${value:.{decimals}f}".replace(".", ",")


def _hora_colombia(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=COLOMBIA).strftime("%Y-%m-%d %H:%M")


def format_report(dias: int, now: float | None = None) -> str:
    now = now or time.time()
    desde = now - dias * 24 * 3600
    conversations = conversation_costs(desde)
    lines = [f"Costo real: últimos {dias} días (desde {_hora_colombia(desde)}, hora de Colombia)", ""]
    if not conversations:
        return "\n".join([*lines, "Sin uso registrado en el periodo."])

    lines.append(f"{'Cliente':<10}{'Inicio':<18}{'Claude':>7}{'Entrada':>9}{'Caché esc.':>11}{'Caché leída':>12}"
                 f"{'Salida':>8}{'Costo Claude':>14}{'Mensajes':>10}")
    for c in conversations:
        # Solo los últimos 4 dígitos: el reporte se puede compartir sin exponer el número del cliente.
        cliente = f"…{c['remitente'][-4:]}" if c["remitente"] else "(sin dato)"
        lines.append(
            f"{cliente:<10}{_hora_colombia(c['inicio']):<18}{c['llamadas_claude']:>7}{c['entrada']:>9}"
            f"{c['cache_escritura']:>11}{c['cache_lectura']:>12}{c['salida']:>8}{_usd(c['costo_claude']):>14}{c['mensajes']:>10}"
        )

    total_claude = sum(c["costo_claude"] for c in conversations)
    total_mensajes = sum(c["mensajes"] for c in conversations)
    n = len(conversations)
    lines += [
        "",
        f"Conversaciones: {n}",
        f"Claude: {_usd(total_claude, 2)} en total · {_usd(total_claude / n)} por conversación",
        f"Mensajes enviados: {total_mensajes} · {total_mensajes / n:.1f} por conversación".replace(".", ","),
        "",
        f"WhatsApp (mensajes de servicio, {SERVICIO_GRATIS_MES} gratis al mes, {_usd(TARIFA_SERVICIO_USD)} c/u después):",
    ]
    for m in whatsapp_costs_by_month(desde):
        lines.append(f"  {m['mes']}: {m['enviados']} enviados · {m['cobrables']} cobrables · {_usd(m['costo'], 2)}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Costo real de Claude y WhatsApp por conversación")
    parser.add_argument("--dias", type=int, default=30, help="días hacia atrás (por defecto 30)")
    print(format_report(parser.parse_args().dias))
