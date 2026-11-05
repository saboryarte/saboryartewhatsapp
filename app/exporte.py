"""Exporte diario de pedidos para el negocio: un Excel por correo y en la carpeta de pedidos de Google Drive.

En el servidor (cron, todos los días a las 7:00 p. m.):
    .venv/bin/python -m app.exporte diario
Otro día:
    .venv/bin/python -m app.exporte diario --fecha 2026-11-04

El Excel trae dos pestañas: los pedidos del día y el acumulado del mes. Los pedidos viven en la tabla «pedidos» de
data/conversaciones.db (en el servidor); este archivo es una copia para leer, no la fuente.
"""
import argparse
import asyncio
import logging
import subprocess
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .archivo import pedidos_dir
from .config import settings
from .flows import format_price, get_product
from .handoff import COLOMBIA
from .notify import notify_team
from .pedidos import ANULADO, ESTADOS, MEDIOS, PAGADO, Pedido, orders_between

logger = logging.getLogger(__name__)

_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_COLUMNAS = (
    ("Referencia", 12), ("Fecha", 17), ("Cliente", 22), ("WhatsApp", 15), ("Productos", 48), ("Subtotal", 11),
    ("Envío", 10), ("Total", 11), ("Medio de pago", 15), ("Estado", 24), ("Pagado el", 17),
    ("Recibe", 22), ("Dirección", 30), ("Ciudad", 14), ("Teléfono", 13),
)
_DINERO = '"$"#,##0'


def exportes_dir() -> Path:
    return pedidos_dir() / "Exportes"


def _hora(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, tz=COLOMBIA).strftime("%Y-%m-%d %H:%M") if ts else ""


def _productos(order: Pedido) -> str:
    partes = []
    for item in order.items:
        if "kit" in item:
            partes.append(f"Kit de {len(item['kit'])} ({', '.join(get_product(m)['nombre'] for m in item['kit'])})")
        else:
            partes.append(f"{item['cantidad']} × {get_product(item['producto'])['nombre']}")
    return "; ".join(partes)


def _row(order: Pedido) -> list:
    envio = order.envio
    return [
        order.referencia, _hora(order.creado_en), order.nombre or "", order.remitente, _productos(order),
        order.subtotal, order.valor_envio, order.total, MEDIOS.get(order.medio_pago, order.medio_pago),
        ESTADOS.get(order.estado, order.estado), _hora(order.pagado_en),
        envio.get("nombre", ""), envio.get("direccion", ""), envio.get("ciudad", ""), envio.get("telefono", ""),
    ]


def _sheet(libro: Workbook, title: str, orders: list[Pedido], first: bool = False) -> None:
    hoja = libro.active if first else libro.create_sheet()
    hoja.title = title
    hoja.append([name for name, _ in _COLUMNAS])
    for celda in hoja[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="5B7F3A")
    for order in orders:
        hoja.append(_row(order))
        for col in (6, 7, 8):
            hoja.cell(hoja.max_row, col).number_format = _DINERO
    pagados = [o for o in orders if o.estado == PAGADO]
    hoja.append([])
    hoja.append(["", "", "", "", f"{len(orders)} pedidos · {len(pagados)} pagados", "", "", sum(o.total for o in pagados)])
    hoja.cell(hoja.max_row, 8).number_format = _DINERO
    hoja.cell(hoja.max_row, 5).font = hoja.cell(hoja.max_row, 8).font = Font(bold=True)
    for i, (_, width) in enumerate(_COLUMNAS, start=1):
        hoja.column_dimensions[hoja.cell(1, i).column_letter].width = width
    hoja.freeze_panes = "B2"


def _day_bounds(day: date) -> tuple[float, float]:
    start = datetime.combine(day, dtime.min, tzinfo=COLOMBIA)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def build_export(day: date) -> tuple[Path, list[Pedido]]:
    start, end = _day_bounds(day)
    of_day = orders_between(start, end)
    month_start, _ = _day_bounds(day.replace(day=1))
    of_month = orders_between(month_start, end)
    libro = Workbook()
    _sheet(libro, f"{day.day} {_MESES[day.month - 1]}", of_day, first=True)
    _sheet(libro, f"Mes de {_MESES[day.month - 1]}", of_month)
    path = exportes_dir() / f"Pedidos {day.isoformat()}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    libro.save(path)
    return path, of_day


def summary(day: date, orders: list[Pedido]) -> tuple[str, str]:
    fecha = f"{day.day} {_MESES[day.month - 1]} {day.year}"
    vigentes = [o for o in orders if o.estado != ANULADO]
    pagados = [o for o in vigentes if o.estado == PAGADO]
    pendientes = [o for o in vigentes if o.estado != PAGADO]
    if not vigentes:
        return f"[Saboryarte] 📊 Pedidos del {fecha}: sin pedidos", f"Hoy ({fecha}) no hubo pedidos nuevos."
    subject = (f"[Saboryarte] 📊 Pedidos del {fecha}: {len(vigentes)} pedido(s), {len(pagados)} pagado(s) "
               f"por {format_price(sum(o.total for o in pagados))}")
    lines = [f"Pedidos del {fecha}. El detalle está en el Excel adjunto (pestaña del día y acumulado del mes).", ""]
    lines += [f"• {o.referencia} · {o.nombre or o.remitente} · {format_price(o.total)} · {MEDIOS.get(o.medio_pago)} · "
              f"{ESTADOS.get(o.estado, o.estado)}" for o in vigentes]
    if pendientes:
        lines += ["", f"Sin pagar todavía: {len(pendientes)}. Revisa los que esperan comprobante o están por verificar."]
    return subject, "\n".join(lines)


def daily(day: date | None = None) -> int:
    day = day or datetime.now(tz=COLOMBIA).date()
    path, orders = build_export(day)
    subject, body = summary(day, orders)
    sent = asyncio.run(notify_team(subject, body, attachments=[path]))
    if settings.pedidos_remoto:
        try:
            subprocess.run(["rclone", "copy", str(pedidos_dir()), settings.pedidos_remoto],
                           check=True, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("Exporte: no se pudo copiar a Drive (%s); se copia en el respaldo de la noche", type(e).__name__)
    logger.info("Exporte del %s: %d pedidos, %s", day, len(orders), "enviado por correo" if sent else "correo NO enviado")
    return 0 if sent else 1


def main() -> None:
    logging.basicConfig(level=settings.log_level)
    parser = argparse.ArgumentParser(description="Exporte diario de pedidos de Saboryarte")
    sub = parser.add_subparsers(dest="accion", required=True)
    diario = sub.add_parser("diario", help="crea el Excel del día y lo envía al equipo")
    diario.add_argument("--fecha", type=date.fromisoformat, help="AAAA-MM-DD (por defecto, hoy en Colombia)")
    args = parser.parse_args()
    sys.exit(daily(args.fecha))


if __name__ == "__main__":
    main()
