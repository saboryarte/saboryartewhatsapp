"""Cálculos del pedido: subtotal, envío y resumen (P8). Todo en código, nunca en Claude.

Un ítem del pedido es {"producto": id, "cantidad": n} (mezcla o kit mini) o {"kit": [ids de mezclas]} (kit armado).
"""
import re
import unicodedata
from typing import Any

from .flows import format_price, get_product, kit_price

# Tarifas de envío estándar (docs/knowledge-base/saboryarte.md). Una persona confirma el valor final.
_TARIFA_MEDELLIN = 9_000
_TARIFA_AREA_METROPOLITANA = 12_000
_TARIFA_NACIONAL = 18_000
_AREA_METROPOLITANA = ("bello", "envigado", "itagui", "sabaneta", "la estrella")

REQUIRED_SHIPPING_FIELDS = ("nombre", "direccion", "ciudad", "telefono")
_FIELD_LABELS = {"nombre": "tu nombre completo", "direccion": "la dirección", "ciudad": "la ciudad", "telefono": "un teléfono de contacto"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _mentions(city: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", _normalize(city)) is not None


def shipping_cost(city: str) -> int:
    if _mentions(city, "medellin"):
        return _TARIFA_MEDELLIN
    if any(_mentions(city, name) for name in _AREA_METROPOLITANA):
        return _TARIFA_AREA_METROPOLITANA
    return _TARIFA_NACIONAL


def item_total(item: dict[str, Any]) -> int:
    if "kit" in item:
        return kit_price(len(item["kit"])) or 0
    return get_product(item["producto"])["precio"] * item["cantidad"]


def subtotal(items: list[dict[str, Any]]) -> int:
    return sum(item_total(item) for item in items)


def describe_item(item: dict[str, Any]) -> str:
    if "kit" in item:
        names = ", ".join(get_product(mix)["nombre"] for mix in item["kit"])
        return f"• Kit de {len(item['kit'])} mezclas ({names}) — {format_price(item_total(item))}"
    product = get_product(item["producto"])
    return f"• {item['cantidad']} × {product['nombre']} — {format_price(item_total(item))}"


def missing_shipping_fields(shipping: dict[str, str | None]) -> list[str]:
    return [field for field in REQUIRED_SHIPPING_FIELDS if not shipping.get(field)]


def ask_missing_shipping(missing: list[str]) -> str:
    labels = [_FIELD_LABELS[field] for field in missing]
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " y " + labels[-1]
    return f"¡Gracias! Para el envío me falta {joined}. ¿Me lo compartes? 😊"


def order_summary(items: list[dict[str, Any]], shipping: dict[str, str]) -> str:
    """Cuerpo del mensaje P8 con los valores calculados."""
    sub = subtotal(items)
    envio = shipping_cost(shipping["ciudad"])
    lines = "\n".join(describe_item(item) for item in items)
    return (
        f"Este es tu pedido:\n{lines}\n\n"
        f"Subtotal: {format_price(sub)}\n"
        f"Envío: {format_price(envio)} ({shipping['ciudad']})\n"
        f"*Total: {format_price(sub + envio)}*\n\n"
        f"Envío a: {shipping['nombre']}, {shipping['direccion']}, {shipping['ciudad']} · Tel. {shipping['telefono']}\n"
        "_Una persona del equipo confirma el valor final del envío._\n\n"
        "¿Todo bien?"
    )


_NUMEROS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
            "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "quince": 15, "veinte": 20}


def parse_quantity(text: str) -> int | None:
    """Cantidad escrita sola («4», «cuatro», «5 unidades»). None si el mensaje dice otra cosa."""
    match = re.fullmatch(r"(\w+)(?: (?:unidades?|und|u|frascos?))?", _normalize(text).strip(" .!"))
    if not match:
        return None
    word = match.group(1)
    quantity = int(word) if word.isdigit() else _NUMEROS.get(word)
    return quantity if quantity and 1 <= quantity <= 100 else None


def merge_items(base: list[dict], new: list[dict]) -> list[dict]:
    """Suma cantidades del mismo producto; los kits armados se agregan aparte."""
    merged = [dict(item) for item in base]
    for item in new:
        same = next((m for m in merged if "producto" in item and m.get("producto") == item["producto"]), None)
        if same:
            same["cantidad"] += item["cantidad"]
        else:
            merged.append(dict(item))
    return merged
