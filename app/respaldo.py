"""Copia de seguridad diaria: base de datos cifrada a Google Drive (60 días) y chats de pedidos legibles (1 año).

En el servidor (cron, todos los días a las 3:00 a. m.):
    .venv/bin/python -m app.respaldo diario
Probar que una copia se puede restaurar:
    .venv/bin/python -m app.respaldo probar data/respaldos/saboryarte_2026-11-04_0300.db

La subida usa rclone (https://rclone.org), configurado una vez en el servidor (docs/despliegue/respaldos.md):
- RESPALDO_REMOTO: remoto «crypt» de rclone sobre Google Drive. Cifra la base antes de subirla: tiene datos de clientes.
- PEDIDOS_REMOTO:  carpeta normal de Drive, compartida solo con el equipo, con un .txt legible por pedido pagado.
Si algo falla, se avisa al equipo por correo.
"""
import argparse
import asyncio
import logging
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .archivo import pedidos_dir
from .config import settings
from .handoff import COLOMBIA
from .notify import notify_team

logger = logging.getLogger(__name__)

DIAS_EN_DRIVE = 60
DIAS_PEDIDOS_EN_DRIVE = 365
# En el servidor se guardan solo las últimas copias: las de 60 días están en Drive.
DIAS_EN_SERVIDOR = 7


def respaldos_dir() -> Path:
    return Path(settings.state_db_path).parent / "respaldos"


def create_backup(now: float | None = None) -> Path:
    """Copia consistente de la base aunque el bot esté atendiendo (API de backup de SQLite), y la verifica."""
    stamp = datetime.fromtimestamp(now or time.time(), tz=COLOMBIA).strftime("%Y-%m-%d_%H%M")
    destino = respaldos_dir() / f"saboryarte_{stamp}.db"
    destino.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.state_db_path)) as origen, closing(sqlite3.connect(destino)) as copia:
        origen.backup(copia)
    check_backup(destino)
    validos = Path(settings.state_db_path).parent / "precios_validos.json"
    if validos.exists():
        shutil.copy2(validos, respaldos_dir() / f"precios_validos_{stamp}.json")
    return destino


def check_backup(path: Path) -> dict[str, int]:
    """Abre la copia como lo haría una restauración: integridad y filas por tabla. Lanza si está dañada."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"la copia {path.name} está dañada: {result}")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


def prune_local(now: float | None = None) -> int:
    limit = (now or time.time()) - DIAS_EN_SERVIDOR * 24 * 3600
    borrados = 0
    for path in respaldos_dir().glob("*"):
        if path.is_file() and path.stat().st_mtime < limit:
            path.unlink()
            borrados += 1
    return borrados


def _rclone(*args: str) -> None:
    subprocess.run(["rclone", *args], check=True, capture_output=True, text=True, timeout=1800)


def upload() -> list[str]:
    """Sube las copias y los chats de pedidos, y borra en Drive lo que ya pasó su plazo. Devuelve lo que hizo."""
    hecho = []
    if settings.respaldo_remoto:
        _rclone("copy", str(respaldos_dir()), settings.respaldo_remoto)
        _rclone("delete", settings.respaldo_remoto, "--min-age", f"{DIAS_EN_DRIVE}d")
        hecho.append(f"copias cifradas en {settings.respaldo_remoto} (se guardan {DIAS_EN_DRIVE} días)")
    if settings.pedidos_remoto and pedidos_dir().exists():
        # copy, no sync: si un archivo se borra en el servidor, la copia de Drive dura hasta cumplir el año.
        _rclone("copy", str(pedidos_dir()), settings.pedidos_remoto)
        _rclone("delete", settings.pedidos_remoto, "--min-age", f"{DIAS_PEDIDOS_EN_DRIVE}d")
        hecho.append(f"chats de pedidos en {settings.pedidos_remoto} (se guardan {DIAS_PEDIDOS_EN_DRIVE} días)")
    return hecho


def daily() -> int:
    try:
        path = create_backup()
        filas = check_backup(path)
        prune_local()
        hecho = upload()
    except Exception as e:  # cualquier falla debe llegar al equipo, no quedarse en el log del cron
        detalle = e.stderr.strip()[-500:] if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
        logger.error("Respaldo fallido: %s", detalle)
        asyncio.run(notify_team(
            "[Saboryarte] ⚠️ Falló la copia de seguridad",
            f"La copia de seguridad de hoy no se completó:\n\n{type(e).__name__}: {detalle}\n\n"
            "El bot sigue funcionando. Revisa el servidor o avísale a quien lo administra.",
        ))
        return 1
    if not hecho:
        logger.warning("Respaldo solo local: falta configurar RESPALDO_REMOTO y PEDIDOS_REMOTO")
    logger.info("Respaldo listo: %s (%s). %s", path.name, ", ".join(f"{t} {n}" for t, n in filas.items()),
                "; ".join(hecho) or "sin subir a Drive")
    return 0


def main() -> None:
    logging.basicConfig(level=settings.log_level)
    parser = argparse.ArgumentParser(description="Copias de seguridad de Saboryarte")
    sub = parser.add_subparsers(dest="accion", required=True)
    sub.add_parser("diario", help="crea la copia del día, la verifica y la sube a Drive")
    probar = sub.add_parser("probar", help="verifica que una copia se puede restaurar")
    probar.add_argument("ruta", type=Path)
    args = parser.parse_args()
    if args.accion == "diario":
        sys.exit(daily())
    for table, count in check_backup(args.ruta).items():
        print(f"{table}: {count} filas")
    print("La copia está completa y se puede restaurar.")


if __name__ == "__main__":
    main()
