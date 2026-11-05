"""Pago por transferencia: comprobantes del cliente y botones del equipo para confirmar o rechazar el pago.

Sin botones (falta PUBLIC_BASE_URL o TRANSFER_LINK_SECRET), desde el servidor:
    .venv/bin/python -m app.transferencias confirmar SAB-00003
    .venv/bin/python -m app.transferencias rechazar SAB-00003

1. El cliente elige «Transferencia» en el resumen: el pedido queda «esperando comprobante» (P11T).
2. El cliente envía la foto o el PDF: se guarda en data/pedidos/Comprobantes/ (se copia a Drive con los pedidos,
   1 año) y le llega al equipo por correo, adjunto, con dos botones: «Confirmar pago» y «No llegó».
3. Los botones abren una página del bot (firmada con TRANSFER_LINK_SECRET) con un botón para confirmar la acción.
   Se pide ese segundo toque a propósito: algunos correos abren los enlaces solos para revisarlos, y un enlace
   que confirmara el pago al abrirse podría darlo por pagado sin que nadie lo viera.
"""
import hashlib
import hmac
from datetime import datetime
from pathlib import Path

from .archivo import pedidos_dir
from .config import settings
from .handoff import COLOMBIA

CONFIRMAR, RECHAZAR = "confirmar", "rechazar"
ACCIONES = {CONFIRMAR: "Confirmar pago", RECHAZAR: "No llegó"}

# Tipos de archivo que se aceptan como comprobante y su extensión.
TIPOS_COMPROBANTE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
    "application/pdf": ".pdf",
}


def comprobantes_dir() -> Path:
    return pedidos_dir() / "Comprobantes"


def save_receipt(referencia: str, content: bytes, mime: str) -> Path:
    stamp = datetime.now(tz=COLOMBIA).strftime("%Y-%m-%d %H%M%S")
    path = comprobantes_dir() / f"{stamp} {referencia}{TIPOS_COMPROBANTE[mime]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _token(order_id: int, accion: str) -> str:
    message = f"transferencia:{order_id}:{accion}".encode()
    return hmac.new(settings.transfer_link_secret.encode(), message, hashlib.sha256).hexdigest()


def valid_token(order_id: int, accion: str, token: str) -> bool:
    return bool(settings.transfer_link_secret) and accion in ACCIONES and hmac.compare_digest(_token(order_id, accion), token)


def team_buttons(order_id: int) -> list[tuple[str, str]]:
    """Botones del correo del comprobante. Vacío si falta la dirección pública o la llave."""
    if not (settings.public_base_url and settings.transfer_link_secret):
        return []
    base = settings.public_base_url.rstrip("/")
    return [(label, f"{base}/transferencias/{order_id}/{accion}?t={_token(order_id, accion)}")
            for accion, label in ACCIONES.items()]


def main() -> None:
    import argparse
    import asyncio
    import re

    from .pedidos import get_order
    from .router import resolve_transfer

    parser = argparse.ArgumentParser(description="Confirmar o rechazar el pago por transferencia de un pedido")
    parser.add_argument("accion", choices=list(ACCIONES))
    parser.add_argument("referencia", help="por ejemplo SAB-00003")
    args = parser.parse_args()
    match = re.fullmatch(r"(?i)SAB-(\d+)", args.referencia.strip())
    order = get_order(int(match.group(1))) if match else None
    if order is None:
        raise SystemExit(f"No existe el pedido {args.referencia}")
    print(asyncio.run(resolve_transfer(order, args.accion)))


if __name__ == "__main__":
    main()
