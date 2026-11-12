"""Página pública de la política de privacidad."""
from fastapi.testclient import TestClient

from app import privacidad
from app.main import app


def test_la_politica_se_publica_en_html():
    r = TestClient(app).get("/privacidad")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "<h1>Política de tratamiento de datos personales de Saboryarte</h1>" in r.text
    assert "Ley 1581 de 2012" in r.text
    assert "BORRADOR" not in r.text  # los comentarios para quien edita no se publican
    assert "<table>" in r.text and "<blockquote>" in r.text


def test_convierte_lo_basico_de_markdown_y_escapa_el_html():
    html = privacidad.to_html("# T\n\nUno **dos** *tres* <script>\n\n- a\n  b\n- c\n\n| x | y |\n|---|---|\n| 1 | 2 |")
    assert "<h1>T</h1>" in html and "<b>dos</b>" in html and "<i>tres</i>" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "<li>a b</li><li>c</li>" in html and "<td>1</td><td>2</td>" in html
