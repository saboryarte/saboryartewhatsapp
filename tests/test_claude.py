import pytest

from app import claude


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("VERIFY_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")


def test_load_business_context_lee_el_archivo_real():
    context = claude._load_business_context()
    assert "Precios" in context
    assert "Flujograma de atención" in context


def test_load_business_context_vacio_si_no_existe(monkeypatch, tmp_path):
    monkeypatch.setattr(claude, "_BUSINESS_DATA_PATH", tmp_path / "no-existe.md")
    assert claude._load_business_context() == ""


def test_system_prompt_incluye_el_contexto_del_negocio(monkeypatch):
    monkeypatch.setattr(claude, "_load_business_context", lambda: "PRECIO ESPECIAL XYZ")
    system = claude._SYSTEM_PROMPT_TEMPLATE.format(business_context=claude._load_business_context())
    assert "PRECIO ESPECIAL XYZ" in system
