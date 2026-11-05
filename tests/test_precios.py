"""Precios desde contenido/precios.yaml: validación y respaldo con los últimos precios válidos."""
import json

import pytest

from app import precios
from app.precios import PRECIOS_PATH, PreciosInvalidos, parse_prices

VALIDO = PRECIOS_PATH.read_text(encoding="utf-8")


def _con(viejo: str, nuevo: str) -> str:
    assert viejo in VALIDO
    return VALIDO.replace(viejo, nuevo, 1)


class TestValidacion:
    def test_el_archivo_del_repo_es_valido(self):
        data = parse_prices(VALIDO)
        assert data["mezclas"]["sal_de_oro"] == 26_000
        assert data["kits_armados"][6] == 133_000
        assert data["envios"]["area_metropolitana"] == 12_000

    @pytest.mark.parametrize("valor, pista", [
        ("$26.000", "no es un número entero"),
        ("26.000", "no es un número entero"),   # YAML lo lee como 26.0
        ("26", "fuera de lo esperado"),          # escrito en miles
        ("260000000", "fuera de lo esperado"),   # ceros de más
        ("", "no es un número entero"),
        ("si", "no es un número entero"),
    ])
    def test_rechaza_precios_mal_escritos(self, valor, pista):
        with pytest.raises(PreciosInvalidos, match=pista) as e:
            parse_prices(_con("sal_de_oro: 26000", f"sal_de_oro: {valor}"))
        assert "mezclas.sal_de_oro" in str(e.value)

    def test_rechaza_una_mezcla_faltante(self):
        with pytest.raises(PreciosInvalidos, match="faltan sal_de_sol"):
            parse_prices(_con("  sal_de_sol: 26000         # Sal de sol, 100 g\n", ""))

    def test_rechaza_un_nombre_cambiado(self):
        with pytest.raises(PreciosInvalidos, match="faltan sal_de_oro"):
            parse_prices(_con("sal_de_oro:", "Sal de Oro:"))

    def test_rechaza_un_kit_faltante(self):
        with pytest.raises(PreciosInvalidos, match="kits_armados: faltan 6_mezclas"):
            parse_prices(_con("  6_mezclas: 133000\n", ""))

    def test_rechaza_yaml_roto_e_indica_la_linea(self):
        with pytest.raises(PreciosInvalidos, match="línea"):
            parse_prices(_con("kit_mini: 60000", "kit_mini: [60000"))


class TestRespaldo:
    @pytest.fixture
    def archivo(self, tmp_path, monkeypatch):
        ruta = tmp_path / "precios.yaml"
        monkeypatch.setattr(precios, "PRECIOS_PATH", ruta)
        precios.current_prices.cache_clear()
        yield ruta
        precios.current_prices.cache_clear()
        precios.load_error = None

    def test_un_archivo_valido_guarda_la_copia(self, archivo, tmp_path):
        archivo.write_text(VALIDO, encoding="utf-8")
        assert precios.current_prices()["kit_mini"] == 60_000
        assert precios.load_error is None
        assert json.loads((tmp_path / "precios_validos.json").read_text())["kit_mini"] == 60_000

    def test_con_un_error_usa_los_ultimos_precios_validos(self, archivo):
        archivo.write_text(_con("kit_mini: 60000", "kit_mini: 65000"), encoding="utf-8")
        precios.current_prices()
        precios.current_prices.cache_clear()
        archivo.write_text(_con("kit_mini: 60000", "kit_mini: 65.000"), encoding="utf-8")
        data = precios.current_prices()
        assert data["kit_mini"] == 65_000 and data["kits_armados"][2] == 48_000
        assert "kit_mini" in precios.load_error

    def test_sin_precios_validos_no_arranca(self, archivo):
        archivo.write_text("mezclas: nada", encoding="utf-8")
        with pytest.raises(RuntimeError, match="no hay precios anteriores"):
            precios.current_prices()
