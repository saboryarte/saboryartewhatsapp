"""Valida app/flows/mensajes.json contra los límites de la API de WhatsApp y la coherencia de las rutas."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MENSAJES = json.loads((ROOT / "app/flows/mensajes.json").read_text(encoding="utf-8"))
PASOS = [paso for flujo in MENSAJES["flujos"].values() for paso in flujo["pasos"]]
POR_ID = {paso["id"]: paso for paso in PASOS}

TIPOS = {"texto", "botones", "lista", "libre", "traspaso", "plantilla", "formulario"}

# Límites de mensajes interactivos de WhatsApp Cloud API
MAX_BOTONES, MAX_TITULO_BOTON = 3, 20
MAX_FILAS, MAX_TITULO_FILA, MAX_DESCRIPCION_FILA, MAX_BOTON_LISTA = 10, 24, 72, 20
MAX_CUERPO_INTERACTIVO, MAX_CUERPO_TEXTO = 1024, 4096

# Datos internos de distribuidores que no deben aparecer en contenido para clientes
DATOS_INTERNOS = ("18.500", "160 por gramo", "2 semanas")


def _largo(texto: str) -> int:
    """Largo en unidades UTF-16: los emojis cuentan doble, así el límite queda del lado seguro."""
    return len(texto.encode("utf-16-le")) // 2


def _opciones(paso: dict) -> list[dict]:
    return paso.get("opciones") or POR_ID.get(paso.get("opciones_de"), {}).get("opciones", [])


def test_ids_de_paso_unicos():
    ids = [paso["id"] for paso in PASOS]
    assert len(ids) == len(set(ids))


def test_ids_de_opcion_unicos():
    ids = [op["id"] for paso in PASOS for op in paso.get("opciones", [])]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("paso", PASOS, ids=lambda p: p["id"])
def test_tipo_valido_y_texto_presente(paso):
    assert paso["tipo"] in TIPOS
    assert paso["texto"].strip()


@pytest.mark.parametrize("paso", [p for p in PASOS if p["tipo"] in ("botones", "plantilla") and "opciones" in p], ids=lambda p: p["id"])
def test_botones_dentro_de_limites(paso):
    assert 1 <= len(paso["opciones"]) <= MAX_BOTONES
    assert _largo(paso["texto"]) <= MAX_CUERPO_INTERACTIVO
    for op in paso["opciones"]:
        assert _largo(op["titulo"]) <= MAX_TITULO_BOTON, op["titulo"]


@pytest.mark.parametrize("paso", [p for p in PASOS if p["tipo"] == "lista"], ids=lambda p: p["id"])
def test_listas_dentro_de_limites(paso):
    opciones = _opciones(paso)
    assert 1 <= len(opciones) <= MAX_FILAS
    assert _largo(paso["texto"]) <= MAX_CUERPO_INTERACTIVO
    assert _largo(paso["boton_lista"]) <= MAX_BOTON_LISTA
    for op in opciones:
        assert _largo(op["titulo"]) <= MAX_TITULO_FILA, op["titulo"]
        assert _largo(op.get("descripcion", "")) <= MAX_DESCRIPCION_FILA, op["descripcion"]


@pytest.mark.parametrize("paso", [p for p in PASOS if p["tipo"] not in ("botones", "lista", "plantilla")], ids=lambda p: p["id"])
def test_texto_dentro_de_limites(paso):
    assert _largo(paso["texto"]) <= MAX_CUERPO_TEXTO


def test_destinos_existen():
    for paso in PASOS:
        if "opciones_de" in paso:
            assert POR_ID[paso["opciones_de"]].get("opciones"), paso["id"]
        for op in paso.get("opciones", []):
            if "va_a" in op:
                assert op["va_a"] in POR_ID, f"{paso['id']} → {op['va_a']}"


def test_mensajes_y_base_publica_sin_datos_de_distribuidor():
    publico = (ROOT / "app/flows/mensajes.json").read_text(encoding="utf-8")
    publico += (ROOT / "docs/knowledge-base/saboryarte.md").read_text(encoding="utf-8")
    for dato in DATOS_INTERNOS:
        assert dato not in publico, dato


def test_productos_referenciados_existen_en_catalogo():
    catalogo = json.loads((ROOT / "app/flows/catalogo.json").read_text(encoding="utf-8"))
    ids_catalogo = {p["id"] for p in catalogo["productos"]} | {catalogo["kit_mini"]["id"]}
    for paso in PASOS:
        for op in paso.get("opciones", []):
            referenciados = [op["producto"]] if "producto" in op else op.get("recomienda", [])
            referenciados += [op["mezcla"]] if "mezcla" in op else []
            for producto in referenciados:
                assert producto in ids_catalogo, f"{op['id']} → {producto}"


INSTAGRAM_URL = "https://www.instagram.com/saboryarte.cocina/"


@pytest.mark.parametrize("paso", [p for p in PASOS if "saboryarte.cocina" in p["texto"].lower() or "nuestro instagram" in p["texto"].lower()], ids=lambda p: p["id"])
def test_instagram_de_saboryarte_lleva_enlace(paso):
    assert INSTAGRAM_URL in paso["texto"]


def test_prompt_y_base_de_conocimiento_llevan_enlace_de_instagram():
    from app.prompt import system_prompt

    assert system_prompt().count(INSTAGRAM_URL) >= 2  # instrucción + base de conocimiento



def test_formulario_del_kit_dentro_de_limites():
    """Botón del Flow: 30 caracteres o menos y sin emojis; opciones del CheckboxGroup: título ≤30, descripción ≤300."""
    paso = POR_ID["P4F"]
    assert len(paso["boton_formulario"]) <= 30 and paso["boton_formulario"].isascii()
    for op in POR_ID["P4C"]["opciones"]:
        assert len(op["titulo"]) <= 30 and len(op["descripcion"]) <= 300
    flow = json.loads((ROOT / "app/flows/kit_flow.json").read_text(encoding="utf-8"))
    pantalla = flow["screens"][0]
    assert pantalla["id"] == "KIT" and pantalla["terminal"] is True
    casillas = pantalla["layout"]["children"][0]["children"][0]
    assert casillas["type"] == "CheckboxGroup"
    assert casillas["min-selected-items"] == casillas["max-selected-items"] == "${data.cantidad}"
