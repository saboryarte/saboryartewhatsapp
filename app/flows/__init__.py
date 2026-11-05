"""Acceso al contenido de los flujos definido en mensajes.json."""
import json
from functools import cache
from pathlib import Path
from typing import Any

from ..precios import current_prices

_MENSAJES_PATH = Path(__file__).with_name("mensajes.json")
_CATALOGO_PATH = Path(__file__).with_name("catalogo.json")

MENU_PRINCIPAL = "G1"

# Respuestas temporales para los pasos que aún no están construidos.
EN_CONSTRUCCION = (
    "¡Listo! Elegiste «{opcion}» 🌿 Esa parte de Chatsabor todavía está en construcción.\n\n"
    "Mientras tanto, cuéntame en un mensaje qué necesitas y con gusto te ayudo."
)
_EN_CONSTRUCCION_POR_PASO = {
    "P11": "¡Gracias! Tu pedido quedó armado ✨ Registrarlo y pasarlo al equipo para el pago y el despacho "
    "todavía está en construcción, así que *este pedido aún no quedó registrado*.\n\n"
    "Mientras tanto, puedes seguir viendo el catálogo o escribir «menú» para volver a empezar.",
    "T1": "Todavía no puedo pasarte con una persona desde este chat 🛠️ Esa parte está en construcción.\n\n"
    "Mientras tanto, cuéntame qué necesitas y te ayudo con gusto.",
}


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


def list_rows(step_id: str) -> list[dict[str, str]]:
    """Filas para send_list a partir de las opciones del paso."""
    rows = []
    for op in step_options(step_id):
        row = {"id": op["id"], "title": op["titulo"]}
        if op.get("descripcion"):
            row["description"] = op["descripcion"]
        rows.append(row)
    return rows


def under_construction(step_id: str, option_title: str) -> str:
    """Respuesta para una opción que lleva a un paso que aún no está construido."""
    return _EN_CONSTRUCCION_POR_PASO.get(step_id) or EN_CONSTRUCCION.format(opcion=option_title)


@cache
def _opciones_por_id() -> dict[str, tuple[str, dict[str, Any]]]:
    """Índice opción_id → (paso_id, opción). Los ids de opción son únicos en todo mensajes.json."""
    return {op["id"]: (step_id, op) for step_id, (_, step) in _pasos().items() for op in step.get("opciones", [])}


def find_any_option(option_id: str) -> tuple[str, dict[str, Any]] | None:
    """Busca una opción en cualquier paso: el cliente puede tocar una lista o botón enviados antes."""
    return _opciones_por_id().get(option_id)


def button_rows(step_id: str) -> list[dict[str, str]]:
    """Botones para send_buttons a partir de las opciones del paso."""
    return [{"id": op["id"], "title": op["titulo"]} for op in step_options(step_id)]


@cache
def _catalogo() -> dict[str, Any]:
    """Nombres y descripciones de catalogo.json con los precios de contenido/precios.yaml."""
    catalogo = json.loads(_CATALOGO_PATH.read_text(encoding="utf-8"))
    prices = current_prices()
    for product in catalogo["productos"]:
        product["precio"] = prices["mezclas"][product["id"]]
    catalogo["kit_mini"]["precio"] = prices["kit_mini"]
    catalogo["kits_armados"] = prices["kits_armados"]
    return catalogo


def get_product(product_id: str) -> dict[str, Any] | None:
    """Busca entre las mezclas y el kit mini."""
    return next((p for p in orderable_products() if p["id"] == product_id), None)


def orderable_products() -> list[dict[str, Any]]:
    """Productos que se pueden pedir por unidad: las 6 mezclas y el kit mini."""
    return [*_catalogo()["productos"], _catalogo()["kit_mini"]]


def mix_ids() -> set[str]:
    """Ids de las 6 mezclas (las que pueden ir en un kit armado)."""
    return {p["id"] for p in _catalogo()["productos"]}


def ordered_mix_ids() -> list[str]:
    """Las 6 mezclas en el orden del catálogo (el kit de 6 las trae todas)."""
    return [p["id"] for p in _catalogo()["productos"]]


def kit_price(size: int) -> int | None:
    return _catalogo()["kits_armados"].get(size)


def format_price(value: int) -> str:
    return "$" + f"{value:,}".replace(",", ".")


def product_card(product: dict[str, Any]) -> str:
    """Texto de la ficha de producto (P2F)."""
    return (
        f"*{product['nombre']}* ✨\n{product['descripcion']}\n\n"
        f"{product['presentacion']}, con cuchara tallada artesanalmente · {format_price(product['precio'])}"
    )


def recommendation(product_ids: list[str]) -> str:
    """Texto con las mezclas recomendadas para un uso (P5)."""
    lines = []
    for product_id in product_ids:
        product = get_product(product_id)
        lines.append(f"• *{product['nombre']}*: {product['descripcion']}")
    return "Te recomiendo estas mezclas 🌿\n\n" + "\n".join(lines)
