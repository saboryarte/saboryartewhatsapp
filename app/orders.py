"""Cálculos del pedido: subtotal, envío y resumen (P8). Todo en código, nunca en Claude.

Un ítem del pedido es {"producto": id, "cantidad": n} (mezcla o kit mini) o {"kit": [ids de mezclas], "cantidad": n}
(n kits armados iguales; los pedidos anteriores no traen «cantidad» y cuentan como 1).
"""
import re
import unicodedata
from typing import Any

from .flows import format_price, get_product, kit_price
from .precios import current_prices

# Las tarifas de envío estándar están en contenido/precios.yaml. Una persona confirma el valor final.
_AREA_METROPOLITANA = ("bello", "envigado", "itagui", "sabaneta", "la estrella")

REQUIRED_SHIPPING_FIELDS = ("nombre", "direccion", "ciudad", "telefono")
_FIELD_LABELS = {"nombre": "tu nombre completo", "direccion": "la dirección", "ciudad": "la ciudad", "telefono": "un teléfono de contacto"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _mentions(city: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", _normalize(city)) is not None


def shipping_zone(city: str) -> str:
    """«medellin», «area_metropolitana» o «resto_del_pais» (las tarifas de contenido/precios.yaml)."""
    if _mentions(city, "medellin"):
        return "medellin"
    if any(_mentions(city, name) for name in _AREA_METROPOLITANA):
        return "area_metropolitana"
    return "resto_del_pais"


def shipping_cost(city: str) -> int:
    return current_prices()["envios"][shipping_zone(city)]


def same_city(a: str, b: str) -> bool:
    """«Medellín» y «Medellin, Antioquia» son la misma ciudad."""
    a, b = " ".join(re.sub(r"[^\w ]", " ", _normalize(a)).split()), " ".join(re.sub(r"[^\w ]", " ", _normalize(b)).split())
    return bool(a and b) and (re.search(rf"\b{re.escape(a)}\b", b) is not None or re.search(rf"\b{re.escape(b)}\b", a) is not None)


def item_quantity(item: dict[str, Any]) -> int:
    return item.get("cantidad", 1)


def item_total(item: dict[str, Any]) -> int:
    if "kit" in item:
        return (kit_price(len(item["kit"])) or 0) * item_quantity(item)
    return get_product(item["producto"])["precio"] * item["cantidad"]


def subtotal(items: list[dict[str, Any]]) -> int:
    return sum(item_total(item) for item in items)


def describe_item(item: dict[str, Any]) -> str:
    if "kit" in item:
        names = ", ".join(get_product(mix)["nombre"] for mix in item["kit"])
        kits = f"{item_quantity(item)} × Kit" if item_quantity(item) > 1 else "Kit"
        return f"• {kits} de {len(item['kit'])} mezclas ({names}) — {format_price(item_total(item))}"
    product = get_product(item["producto"])
    return f"• {item['cantidad']} × {product['nombre']} — {format_price(item_total(item))}"


def missing_shipping_fields(shipping: dict[str, str | None]) -> list[str]:
    return [field for field in REQUIRED_SHIPPING_FIELDS if not shipping.get(field)]


def ask_missing_shipping(missing: list[str]) -> str:
    labels = [_FIELD_LABELS[field] for field in missing]
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " y " + labels[-1]
    return f"¡Gracias! Para el envío me falta {joined}. ¿Me lo compartes? 😊"


def order_summary(items: list[dict[str, Any]], shipping: dict[str, str], aviso_resto: str = "") -> str:
    """Cuerpo del mensaje P8 con los valores calculados. «aviso_resto» va si el envío es al resto del país."""
    sub = subtotal(items)
    envio = shipping_cost(shipping["ciudad"])
    lines = "\n".join(describe_item(item) for item in items)
    aviso = f" _{aviso_resto}_" if aviso_resto and shipping_zone(shipping["ciudad"]) == "resto_del_pais" else ""
    return (
        f"Este es tu pedido:\n{lines}\n\n"
        f"Subtotal: {format_price(sub)}\n"
        f"Envío: {format_price(envio)} ({shipping['ciudad']}){aviso}\n"
        f"*Total: {format_price(sub + envio)}*\n\n"
        f"Envío a: {shipping['nombre']}, {shipping['direccion']}, {shipping['ciudad']} · Tel. {shipping['telefono']}\n\n"
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


