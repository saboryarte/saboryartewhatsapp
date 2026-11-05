"""Hoja de precios para la dueña: exportar, editar e importar con la misma validación del bot."""
import pytest
from openpyxl import load_workbook

from app import hoja_precios
from app.precios import PRECIOS_PATH, PreciosInvalidos, parse_prices


@pytest.fixture
def yaml_copia(tmp_path, monkeypatch):
    copia = tmp_path / "precios.yaml"
    copia.write_text(PRECIOS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(hoja_precios, "PRECIOS_PATH", copia)
    return copia


@pytest.fixture
def hoja(tmp_path, yaml_copia):
    ruta = tmp_path / "precios.xlsx"
    hoja_precios.export_sheet(ruta)
    return ruta


def _editar(ruta, cambios: dict[str, object]):
    libro = load_workbook(ruta)
    filas = libro["Precios"]
    for fila in filas.iter_rows(min_row=2):
        if fila[3].value in cambios:
            fila[2].value = cambios[fila[3].value]
    libro.save(ruta)


def test_la_hoja_exportada_trae_todos_los_precios(hoja):
    filas = list(load_workbook(hoja)["Precios"].iter_rows(values_only=True))
    assert filas[0] == ("Sección", "Producto", "Precio", "Clave")
    assert ("Mezclas", "Sal de oro (100 g)", 26000, "sal_de_oro") in filas
    assert ("Kits armados", "Kit de 6 mezclas", 133000, "6_mezclas") in filas
    assert len(filas) == 1 + 6 + 1 + 5 + 3
    assert hoja_precios.import_sheet(hoja, aplicar=False) == []


def test_muestra_los_cambios_sin_aplicarlos(hoja, yaml_copia):
    antes = yaml_copia.read_text(encoding="utf-8")
    _editar(hoja, {"sal_de_oro": 28000, "medellin": "$10.000"})
    assert hoja_precios.import_sheet(hoja, aplicar=False) == [
        "Sal de oro (100 g): $26.000 → $28.000", "Envío a Medellín: $9.000 → $10.000"]
    assert yaml_copia.read_text(encoding="utf-8") == antes


def test_aplicar_escribe_el_yaml_y_conserva_las_notas(hoja, yaml_copia):
    _editar(hoja, {"kit_mini": 65000.0, "2_mezclas": 50000})
    hoja_precios.import_sheet(hoja, aplicar=True)
    texto = yaml_copia.read_text(encoding="utf-8")
    assert "kit_mini: 65000             # Las 6 mezclas en miniatura" in texto
    assert "# Cómo cambiar un precio:" in texto
    precios = parse_prices(texto)
    assert precios["kit_mini"] == 65000 and precios["kits_armados"][2] == 50000


@pytest.mark.parametrize("valor", ["26.5", "veintiséis", None, 26, 260_000_000, True])
def test_rechaza_precios_que_no_sirven(hoja, yaml_copia, valor):
    antes = yaml_copia.read_text(encoding="utf-8")
    _editar(hoja, {"sal_de_sol": valor})
    with pytest.raises(PreciosInvalidos):
        hoja_precios.import_sheet(hoja, aplicar=True)
    assert yaml_copia.read_text(encoding="utf-8") == antes


def test_rechaza_una_fila_borrada(hoja):
    libro = load_workbook(hoja)
    libro["Precios"].delete_rows(3)
    libro.save(hoja)
    with pytest.raises(PreciosInvalidos, match="faltan filas en la hoja: Sal de sol"):
        hoja_precios.import_sheet(hoja, aplicar=False)


def test_rechaza_un_archivo_que_no_es_la_hoja(tmp_path, yaml_copia):
    otro = tmp_path / "otro.xlsx"
    otro.write_text("no soy un excel")
    with pytest.raises(PreciosInvalidos, match="no se pudo abrir"):
        hoja_precios.import_sheet(otro, aplicar=False)
