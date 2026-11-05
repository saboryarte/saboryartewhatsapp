"""Precios al público desde contenido/precios.yaml, el archivo que la dueña edita en GitHub.

Es la única fuente de precios: de aquí salen el cobro en Bold, los resúmenes del pedido y lo que Claude responde.
Como un error de escritura llegaría directo al cobro, el archivo se valida completo al cargarlo. Si no pasa,
se usan los últimos precios válidos (guardados en data/) y se avisa al equipo; sin precios válidos el bot no arranca.
"""
import json
import logging
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from .config import settings

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
PRECIOS_PATH = _ROOT / "contenido" / "precios.yaml"
_CATALOGO_PATH = Path(__file__).with_name("flows") / "catalogo.json"

_KIT_SIZES = range(2, 7)
_ENVIOS = ("medellin", "area_metropolitana", "resto_del_pais")
# Límites de cordura: atrapan un cero de más o de menos, o un precio en miles («26» en vez de «26000»).
_MINIMO, _MAXIMO = 1_000, 2_000_000

# Error de la última carga, si la hubo: se le avisa al equipo al arrancar.
load_error: str | None = None


class PreciosInvalidos(ValueError):
    pass


def _mix_ids() -> list[str]:
    catalogo = json.loads(_CATALOGO_PATH.read_text(encoding="utf-8"))
    return [p["id"] for p in catalogo["productos"]]


def _price(value: Any, where: str) -> int:
    # bool es un int en Python: «true» no puede pasar por un precio.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreciosInvalidos(f"{where}: «{value}» no es un número entero (escríbelo sin puntos, comas ni $)")
    if not _MINIMO <= value <= _MAXIMO:
        raise PreciosInvalidos(f"{where}: {value} está fuera de lo esperado (entre {_MINIMO} y {_MAXIMO})")
    return value


def _section(raw: dict, name: str, expected: list[str]) -> dict[str, int]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise PreciosInvalidos(f"falta la sección «{name}»")
    missing = [key for key in expected if key not in section]
    extra = [str(key) for key in section if key not in expected]
    if missing:
        raise PreciosInvalidos(f"{name}: faltan {', '.join(missing)}")
    if extra:
        raise PreciosInvalidos(f"{name}: no se reconoce {', '.join(extra)}")
    return {key: _price(section[key], f"{name}.{key}") for key in expected}


def parse_prices(text: str) -> dict[str, Any]:
    """Valida el contenido de precios.yaml y lo devuelve normalizado. Lanza PreciosInvalidos con un mensaje claro."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f" (línea {mark.line + 1})" if mark else ""
        raise PreciosInvalidos(f"el archivo no se puede leer{line}: revisa los dos puntos y los espacios") from None
    if not isinstance(raw, dict):
        raise PreciosInvalidos("el archivo está vacío o no tiene el formato esperado")
    kits = _section(raw, "kits_armados", [f"{n}_mezclas" for n in _KIT_SIZES])
    return {
        "mezclas": _section(raw, "mezclas", _mix_ids()),
        "kit_mini": _price(raw.get("kit_mini"), "kit_mini"),
        "kits_armados": {n: kits[f"{n}_mezclas"] for n in _KIT_SIZES},
        "envios": _section(raw, "envios", list(_ENVIOS)),
    }


def _snapshot_path() -> Path:
    return Path(settings.state_db_path).parent / "precios_validos.json"


def _save_snapshot(prices: dict[str, Any]) -> None:
    path = _snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prices, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("No se pudo guardar la copia de los precios válidos")


def _load_snapshot() -> dict[str, Any] | None:
    try:
        data = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["kits_armados"] = {int(n): v for n, v in data["kits_armados"].items()}
    return data


@cache
def current_prices() -> dict[str, Any]:
    """Precios vigentes. Se leen una vez: un cambio en precios.yaml se aplica al reiniciar el bot."""
    global load_error
    try:
        prices = parse_prices(PRECIOS_PATH.read_text(encoding="utf-8"))
    except (OSError, PreciosInvalidos) as e:
        load_error = str(e)
        fallback = _load_snapshot()
        if fallback is None:
            raise RuntimeError(f"contenido/precios.yaml no es válido y no hay precios anteriores: {e}") from e
        logger.error("precios.yaml no es válido (%s): se usan los últimos precios válidos", e)
        return fallback
    load_error = None
    _save_snapshot(prices)
    return prices
