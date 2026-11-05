"""Copia de seguridad diaria: copia verificada de la base, limpieza local y subida a Drive con rclone."""
import os
import subprocess
import time
from unittest.mock import AsyncMock

import pytest

from app import archivo, respaldo
from app.config import settings

DIA = 24 * 3600


@pytest.fixture
def base():
    archivo.record("573001112233", archivo.CLIENTE, "hola")
    archivo.save_order_chat(1, "SAB-00001", "573001112233", "Laura", "ficha")


@pytest.fixture
def rclone(monkeypatch):
    llamadas = []
    monkeypatch.setattr(respaldo, "_rclone", lambda *args: llamadas.append(args))
    return llamadas


def test_la_copia_se_puede_restaurar(base):
    copia = respaldo.create_backup()
    filas = respaldo.check_backup(copia)
    assert filas["chat"] == 1 and filas["chats_pedidos"] == 1


def test_una_copia_danada_se_detecta(tmp_path):
    danada = tmp_path / "danada.db"
    danada.write_bytes(b"no es una base de datos" * 100)
    with pytest.raises(Exception):
        respaldo.check_backup(danada)


def test_en_el_servidor_solo_quedan_7_dias(base):
    vieja = respaldo.create_backup(time.time() - 8 * DIA)
    os.utime(vieja, (time.time() - 8 * DIA,) * 2)
    nueva = respaldo.create_backup()
    assert respaldo.prune_local() == 1
    assert not vieja.exists() and nueva.exists()


def test_sube_cifrado_60_dias_y_pedidos_1_ano(base, rclone, monkeypatch):
    monkeypatch.setattr(settings, "respaldo_remoto", "drive-cifrado:respaldos")
    monkeypatch.setattr(settings, "pedidos_remoto", "drive:Saboryarte/Pedidos")
    assert respaldo.daily() == 0
    assert ("delete", "drive-cifrado:respaldos", "--min-age", "60d") in rclone
    assert ("delete", "drive:Saboryarte/Pedidos", "--min-age", "365d") in rclone
    assert rclone[0] == ("copy", str(respaldo.respaldos_dir()), "drive-cifrado:respaldos")


def test_sin_remotos_queda_solo_la_copia_local(base, rclone):
    assert respaldo.daily() == 0
    assert rclone == [] and list(respaldo.respaldos_dir().glob("*.db"))


def test_si_falla_la_subida_avisa_al_equipo(base, monkeypatch):
    monkeypatch.setattr(settings, "respaldo_remoto", "drive-cifrado:respaldos")
    def falla(*args):
        raise subprocess.CalledProcessError(1, "rclone", stderr="Failed to copy: token expired")
    monkeypatch.setattr(respaldo, "_rclone", falla)
    aviso = AsyncMock(return_value=True)
    monkeypatch.setattr(respaldo, "notify_team", aviso)
    assert respaldo.daily() == 1
    asunto, cuerpo = aviso.call_args.args
    assert "Falló la copia" in asunto and "token expired" in cuerpo
