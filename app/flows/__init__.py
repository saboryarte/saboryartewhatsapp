"""Acceso al contenido de los flujos definido en mensajes.json."""
import json
from functools import cache
from pathlib import Path
from typing import Any

_MENSAJES_PATH = Path(__file__).with_name("mensajes.json")

MENU_PRINCIPAL = "G1"

# Respuesta temporal mientras las rutas del menú no estén construidas (fase 1).
EN_CONSTRUCCION = (
    "¡Listo! Elegiste «{opcion}» 🌿 Esa parte del asistente todavía está en construcción.\n\n"
    "Mientras tanto, cuéntame en un mensaje qué necesitas y con gusto te ayudo."
)


@cache
def _flujos() -> dict[str, Any]:
    return json.loads(_MENSAJES_PATH.read_text(encoding="utf-8"))["flujos"]


@cache
def _pasos() -> dict[str, tuple[str, dict[str, Any]]]:
    """Índice paso_id → (nombre del flujo, paso)."""
    return {paso["id"]: (nombre, paso) for nombre, flujo in _flujos().items() for paso in flujo["pasos"]}


def get_step(step_id: str) -> dict[str, Any]:
    return _pasos()[step_id][1]


def flow_of(step_id: str) -> str:
    return _pasos()[step_id][0]


def step_options(step_id: str) -> list[dict[str, Any]]:
    """Opciones del paso; si usa «opciones_de», las del paso referenciado."""
    step = get_step(step_id)
    if "opciones_de" in step:
        step = get_step(step["opciones_de"])
    return step.get("opciones", [])


def find_option(step_id: str, option_id: str) -> dict[str, Any] | None:
    return next((op for op in step_options(step_id) if op["id"] == option_id), None)


def list_rows(step_id: str) -> list[dict[str, str]]:
    """Filas para send_list a partir de las opciones del paso."""
    rows = []
    for op in step_options(step_id):
        row = {"id": op["id"], "title": op["titulo"]}
        if op.get("descripcion"):
            row["description"] = op["descripcion"]
        rows.append(row)
    return rows
