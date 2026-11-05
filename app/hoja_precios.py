"""Hoja de cálculo de precios para la dueña: se exporta desde contenido/precios.yaml y se importa de vuelta.

Exportar la hoja actual:   .venv/bin/python -m app.hoja_precios exportar exportes/precios_saboryarte.xlsx
Ver qué cambiaría:         .venv/bin/python -m app.hoja_precios importar ruta/a/la/hoja.xlsx
Aplicar los cambios:       .venv/bin/python -m app.hoja_precios importar ruta/a/la/hoja.xlsx --aplicar

La hoja tiene una columna «Clave» (gris) que dice a qué línea de precios.yaml va cada precio: solo se cambia la
columna «Precio». Antes de escribir nada, el archivo nuevo pasa por la misma validación que usa el bot.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.worksheet.datavalidation import DataValidation

from .flows import format_price
from .precios import PRECIOS_PATH, PreciosInvalidos, parse_prices

_CATALOGO_PATH = Path(__file__).with_name("flows") / "catalogo.json"
_HOJA = "Precios"
_COLUMNAS = ("Sección", "Producto", "Precio", "Clave")
_ENVIOS = {
    "medellin": "Envío a Medellín",
    "area_metropolitana": "Envío a Bello, Envigado, Itagüí, Sabaneta y La Estrella",
    "resto_del_pais": "Envío al resto del país",
}


def _filas(prices: dict) -> list[tuple[str, str, int, str]]:
    """(sección, producto, precio, clave) en el orden de precios.yaml. La clave es el nombre de la línea del YAML."""
    catalogo = json.loads(_CATALOGO_PATH.read_text(encoding="utf-8"))
    nombres = {p["id"]: f"{p['nombre']} ({p['presentacion']})" for p in catalogo["productos"]}
    filas = [("Mezclas", nombres[key], value, key) for key, value in prices["mezclas"].items()]
    filas.append(("Kit mini", "Kit mini (las 6 mezclas en miniatura)", prices["kit_mini"], "kit_mini"))
    filas += [("Kits armados", f"Kit de {n} mezclas", v, f"{n}_mezclas") for n, v in prices["kits_armados"].items()]
    filas += [("Envíos", _ENVIOS[key], value, key) for key, value in prices["envios"].items()]
    return filas


def export_sheet(path: Path) -> None:
    prices = parse_prices(PRECIOS_PATH.read_text(encoding="utf-8"))
    libro = Workbook()
    hoja = libro.active
    hoja.title = _HOJA
    hoja.append(_COLUMNAS)
    for celda in hoja[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="5B7F3A")
    gris = Font(color="9A9A9A")
    editable = PatternFill("solid", fgColor="FFF6D5")
    for fila in _filas(prices):
        hoja.append(fila)
        precio, clave = hoja.cell(hoja.max_row, 3), hoja.cell(hoja.max_row, 4)
        precio.number_format = '"$"#,##0'
        precio.fill = editable
        precio.protection = Protection(locked=False)
        clave.font = gris
    # Solo números enteros en la columna de precio: Excel y Google Sheets avisan si se escribe otra cosa.
    regla = DataValidation(type="whole", operator="between", formula1="1000", formula2="2000000", allow_blank=False)
    regla.error = "Escribe el precio en pesos, solo el número (por ejemplo 26000)."
    regla.errorTitle = "Precio no válido"
    hoja.add_data_validation(regla)
    regla.add(f"C2:C{hoja.max_row}")
    for columna, ancho in zip("ABCD", (16, 52, 14, 22)):
        hoja.column_dimensions[columna].width = ancho
    hoja.freeze_panes = "A2"

    ayuda = libro.create_sheet("Instrucciones")
    for linea in (
        "Cómo actualizar los precios de Saboryarte",
        "",
        "1. Cambia solo la columna «Precio» (amarilla) de la hoja «Precios». Escribe el número sin puntos ni $: 26000.",
        "2. No cambies las columnas «Sección», «Producto» ni «Clave», y no borres filas.",
        "3. Guarda el archivo como Excel (.xlsx). Desde Google Sheets: Archivo → Descargar → Microsoft Excel.",
        "4. Adjunta el archivo en un chat de Claude Code del proyecto y escribe: «Actualiza los precios con esta hoja».",
        "5. Claude te muestra qué precios cambian y te pide confirmación antes de aplicarlos.",
        "",
        "Estos precios se usan para responder a los clientes Y para cobrar con Bold.",
        "Los kits armados tienen precio según cuántas mezclas lleva el kit.",
        "El envío es la tarifa estándar: una persona del equipo confirma el valor final.",
    ):
        ayuda.append([linea])
    ayuda["A1"].font = Font(bold=True, size=13)
    ayuda.column_dimensions["A"].width = 110
    for fila in ayuda.iter_rows():
        fila[0].alignment = Alignment(wrap_text=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    libro.save(path)


def _to_int(value, where: str) -> int:
    """Acepta 26000, 26000.0 o un texto con formato de miles («$26.000», «26,000»). Nada más."""
    if isinstance(value, bool):
        raise PreciosInvalidos(f"{where}: «{value}» no es un precio")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\$?\s*\d{1,3}(?:[.,]\d{3})*|\$?\s*\d+", value.strip()):
        return int(re.sub(r"\D", "", value))
    raise PreciosInvalidos(f"{where}: «{value}» no es un precio (escríbelo solo con números, por ejemplo 26000)")


def read_sheet(path: Path) -> dict[str, int]:
    """Clave → precio de la hoja. Lanza PreciosInvalidos si la hoja no tiene la forma esperada."""
    try:
        libro = load_workbook(path, data_only=True, read_only=True)
    except Exception as e:  # openpyxl lanza tipos variados para archivos que no son .xlsx
        raise PreciosInvalidos(f"no se pudo abrir la hoja ({type(e).__name__}): ¿está guardada como .xlsx?") from None
    if _HOJA not in libro.sheetnames:
        raise PreciosInvalidos(f"la hoja no tiene la pestaña «{_HOJA}»")
    filas = list(libro[_HOJA].iter_rows(values_only=True))
    if not filas or tuple(filas[0][:4]) != _COLUMNAS:
        raise PreciosInvalidos(f"la primera fila debe ser {', '.join(_COLUMNAS)}")
    valores: dict[str, int] = {}
    for numero, fila in enumerate(filas[1:], start=2):
        if not any(fila):
            continue
        clave, producto = fila[3], fila[1]
        if not clave:
            raise PreciosInvalidos(f"fila {numero}: falta la clave")
        if clave in valores:
            raise PreciosInvalidos(f"fila {numero}: la clave «{clave}» está repetida")
        valores[str(clave)] = _to_int(fila[2], f"fila {numero} ({producto})")
    return valores


def apply_to_yaml(text: str, valores: dict[str, int]) -> str:
    """Reemplaza los números de precios.yaml conservando los comentarios. Toda clave de la hoja debe existir."""
    for clave, precio in valores.items():
        text, cambios = re.subn(rf"(?m)^(\s*{re.escape(clave)}:\s*)\S+", rf"\g<1>{precio}", text)
        if cambios != 1:
            raise PreciosInvalidos(f"la clave «{clave}» no existe en precios.yaml")
    return text


def import_sheet(path: Path, aplicar: bool) -> list[str]:
    """Valida la hoja y devuelve los cambios («Producto: antes → después»). Con aplicar=True escribe precios.yaml."""
    actual = PRECIOS_PATH.read_text(encoding="utf-8")
    antes = {fila[3]: (fila[1], fila[2]) for fila in _filas(parse_prices(actual))}
    valores = read_sheet(path)
    faltan = [antes[clave][0] for clave in antes if clave not in valores]
    if faltan:
        raise PreciosInvalidos(f"faltan filas en la hoja: {', '.join(faltan)}")
    nuevo = apply_to_yaml(actual, valores)
    parse_prices(nuevo)  # la misma validación que usa el bot al arrancar
    cambios = [
        f"{antes[clave][0]}: {format_price(antes[clave][1])} → {format_price(precio)}"
        for clave, precio in valores.items() if precio != antes[clave][1]
    ]
    if aplicar and cambios:
        PRECIOS_PATH.write_text(nuevo, encoding="utf-8")
    return cambios


def main() -> None:
    parser = argparse.ArgumentParser(description="Hoja de precios de Saboryarte (exportar e importar)")
    sub = parser.add_subparsers(dest="accion", required=True)
    exportar = sub.add_parser("exportar", help="crea la hoja con los precios actuales")
    exportar.add_argument("ruta", type=Path, nargs="?", default=Path("exportes/precios_saboryarte.xlsx"))
    importar = sub.add_parser("importar", help="valida una hoja editada y muestra o aplica los cambios")
    importar.add_argument("ruta", type=Path)
    importar.add_argument("--aplicar", action="store_true", help="escribe los cambios en contenido/precios.yaml")
    args = parser.parse_args()

    if args.accion == "exportar":
        export_sheet(args.ruta)
        print(f"Hoja creada: {args.ruta}")
        return
    try:
        cambios = import_sheet(args.ruta, args.aplicar)
    except PreciosInvalidos as e:
        print(f"La hoja no se puede usar: {e}", file=sys.stderr)
        sys.exit(1)
    if not cambios:
        print("La hoja es válida y no cambia ningún precio.")
        return
    print(("Precios actualizados en contenido/precios.yaml:" if args.aplicar else "Cambios que se aplicarían:"))
    print("\n".join(f"  • {c}" for c in cambios))
    if not args.aplicar:
        print("\nPara aplicarlos, repite el comando con --aplicar.")


if __name__ == "__main__":
    main()
