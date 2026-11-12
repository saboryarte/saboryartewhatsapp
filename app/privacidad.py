"""Página pública /privacidad: la política de tratamiento de datos de contenido/politica_de_privacidad.md.

El archivo se edita en el repositorio, como los precios. Aquí se convierte a HTML con lo poco de Markdown que usa:
títulos, párrafos, listas, tablas, citas, separadores, **negrita** y *cursiva*. Los comentarios <!-- --> no se muestran.
"""
import html
import re
from functools import cache
from pathlib import Path

POLITICA = Path(__file__).resolve().parent.parent / "contenido" / "politica_de_privacidad.md"


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)


def to_html(markdown: str) -> str:
    lines = re.sub(r"<!--.*?-->", "", markdown, flags=re.S).strip().splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("#"):
            nivel = min(len(line) - len(line.lstrip("#")), 3)
            out.append(f"<h{nivel}>{_inline(line.lstrip('#').strip())}</h{nivel}>")
            i += 1
        elif line.startswith("---"):
            out.append("<hr>")
            i += 1
        elif line.startswith("|"):
            filas = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"^\|\s*-", lines[i]):
                    filas.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            cabeza = "".join(f"<th>{_inline(c)}</th>" for c in filas[0])
            cuerpo = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in f) + "</tr>" for f in filas[1:])
            out.append(f"<div class='tabla'><table><thead><tr>{cabeza}</tr></thead><tbody>{cuerpo}</tbody></table></div>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                item = lines[i][2:]
                i += 1
                while i < len(lines) and re.match(r"^ {2}\S", lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{_inline(item)}</li>")
            out.append(f"<ul>{''.join(items)}</ul>")
        elif line.startswith(">"):
            texto = []
            while i < len(lines) and lines[i].startswith(">"):
                texto.append(lines[i].lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(texto))}</blockquote>")
        else:
            texto = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|- |\||>|---)", lines[i]):
                texto.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(texto))}</p>")
    return "\n".join(out)


@cache
def page() -> str:
    cuerpo = to_html(POLITICA.read_text(encoding="utf-8"))
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Política de privacidad · Saboryarte</title><style>"
        ":root{color-scheme:light dark;--bg:#fbfbf8;--fg:#1f261b;--muted:#5d6656;--acc:#4f7232;--line:#dde2d4}"
        "@media (prefers-color-scheme:dark){:root{--bg:#141811;--fg:#e8ece1;--muted:#a3ad98;--acc:#9cc677;--line:#303a29}}"
        "body{background:var(--bg);color:var(--fg);font:17px/1.6 system-ui,sans-serif;margin:0;"
        "padding:2rem max(16px,env(safe-area-inset-right)) 3rem max(16px,env(safe-area-inset-left))}"
        "main{max-width:44rem;margin:0 auto}h1,h2{color:var(--acc);line-height:1.2}h1{font-size:1.7rem}"
        "h2{font-size:1.2rem;margin-top:2rem}.tabla{overflow-x:auto}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid var(--line);padding:.45rem .6rem;text-align:left;vertical-align:top}"
        "blockquote{margin:1rem 0;padding-left:1rem;border-left:3px solid var(--acc)}"
        "hr{border:0;border-top:1px solid var(--line);margin:2rem 0}"
        f"</style></head><body><main>{cuerpo}</main></body></html>"
    )
