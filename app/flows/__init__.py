"""Acceso al contenido de los flujos definido en mensajes.json."""
import json
from functools import cache
from pathlib import Path
from typing import Any

from ..precios import current_prices

_MENSAJES_PATH = Path(__file__).with_name("mensajes.json")
_CATALOGO_PATH = Path(__file__).with_name("catalogo.json")

MENU_PRINCIPAL = "G1"


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
        title = op["titulo"]
        if "kit" in op and (price := kit_price(op["kit"])):
            # Los precios de los kits salen de contenido/precios.yaml, no del texto guardado en mensajes.json.
            title = f"{op['kit']} mezclas · {format_price(price)}"
        row = {"id": op["id"], "title": title}
        if op.get("descripcion"):
            row["description"] = op["descripcion"]
        rows.append(row)
    return rows


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


def step_text(step_id: str) -> str:
    """Texto del paso con los precios del catálogo (P4: kit mini; P6: las mezclas). Así no se desactualizan."""
    text = get_step(step_id)["texto"]
    if step_id == "P4":
        return text.replace("[precio kit mini]", format_price(_catalogo()["kit_mini"]["precio"]))
    if step_id == "P6":
        mixes = _catalogo()["productos"]
        prices = {p["precio"] for p in mixes}
        if len(prices) == 1:
            lines = "\n".join(f"• {p['nombre']} ({p['presentacion']})" for p in mixes)
            return text.replace("[mezclas]", lines).replace("[precio]", format_price(prices.pop()))
        # Si algún día cambian los precios de una sola mezcla, cada línea lleva el suyo.
        lines = "\n".join(f"• {p['nombre']} ({p['presentacion']}) · {format_price(p['precio'])}" for p in mixes)
        return text.replace("[mezclas]", lines).replace(" y cuesta [precio]", "")
    return text
