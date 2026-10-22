"""Compara modelos en las respuestas libres del bot con los casos de casos_respuestas_libres.json.

Uso:  .venv/bin/python -m evals.comparar_modelos
      .venv/bin/python -m evals.comparar_modelos --modelos claude-sonnet-5-5   (solo esos modelos)
      .venv/bin/python -m evals.comparar_modelos --rejuzgar evals/resultados/<archivo>.json   (solo vuelve a juzgar)

Cada caso corre REPETICIONES veces por modelo con el mismo system prompt de producción (app/prompt.py).
Un intento pasa si cumple las revisiones automáticas y el juez (otro modelo, a ciegas) confirma los criterios.
El gasto se calcula con el uso real de cada respuesta y el script se detiene antes de LIMITE_USD.
"""
import asyncio
import json
import sys
import random
import re
import time
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel

from app.config import settings
from app.prompt import system_prompt

_BASE_CONOCIMIENTO = (Path(__file__).resolve().parent.parent / "docs" / "knowledge-base" / "saboryarte.md").read_text(encoding="utf-8")

MODELOS = ["claude-sonnet-5-5", "claude-haiku-5-5"]
JUEZ = "claude-opus-5-5"
REPETICIONES = 3
ESFUERZO = "low"  # chat de atención: esfuerzo bajo
LIMITE_USD = 0.95
CONCURRENCIA = 6

# US$ por millón de tokens (tabla de precios del 2026-10-06). Escritura de caché = 1,25 × entrada.
PRECIOS = {
    "claude-sonnet-5-5": {"entrada": 2.00, "salida": 10.00, "cache_lectura": 0.20},
    "claude-haiku-5-5": {"entrada": 0.10, "salida": 0.50, "cache_lectura": 0.01},
    "claude-opus-5-5": {"entrada": 4.00, "salida": 20.00, "cache_lectura": 0.20},
}

RAIZ = Path(__file__).resolve().parent
CASOS = json.loads((RAIZ / "casos_respuestas_libres.json").read_text(encoding="utf-8"))

client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
gasto = {"total": 0.0, "por_modelo": {}}


class PresupuestoAgotado(Exception):
    pass


def _registrar_costo(modelo: str, usage) -> float:
    p = PRECIOS[modelo]
    costo = (
        usage.input_tokens * p["entrada"]
        + (usage.cache_creation_input_tokens or 0) * p["entrada"] * 1.25
        + (usage.cache_read_input_tokens or 0) * p["cache_lectura"]
        + usage.output_tokens * p["salida"]
    ) / 1_000_000
    gasto["total"] += costo
    gasto["por_modelo"][modelo] = gasto["por_modelo"].get(modelo, 0.0) + costo
    return costo


def _verificar_presupuesto() -> None:
    if gasto["total"] >= LIMITE_USD:
        raise PresupuestoAgotado(f"Gasto acumulado US${gasto['total']:.3f}")


# ── Respuestas de los candidatos ──────────────────────────────────────────────

async def responder(modelo: str, caso: dict) -> dict:
    _verificar_presupuesto()
    inicio = time.perf_counter()
    respuesta = await client.messages.create(
        model=modelo,
        max_tokens=16000,
        output_config={"effort": ESFUERZO},
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": caso["mensaje"]}],
    )
    latencia = time.perf_counter() - inicio
    texto = "".join(b.text for b in respuesta.content if b.type == "text")
    return {
        "texto": texto,
        "rechazo": respuesta.stop_reason == "refusal",
        "latencia_s": round(latencia, 2),
        "costo_usd": _registrar_costo(modelo, respuesta.usage),
        "tokens_salida": respuesta.usage.output_tokens,
        "cache_leidos": respuesta.usage.cache_read_input_tokens or 0,
    }


# ── Revisiones automáticas ────────────────────────────────────────────────────

_FORMATO_PROHIBIDO = [r"(?m)^#", r"\*\*", r"\|\s*-{3}"]


def revisiones_automaticas(caso: dict, texto: str) -> list[str]:
    fallas = []
    if not texto.strip():
        fallas.append("respuesta vacía")
    for requerido in caso.get("incluir", []):
        if requerido.lower() not in texto.lower():
            fallas.append(f"no incluye «{requerido}»")
    for patron in caso.get("excluir_regex", []):
        if re.search(patron, texto, re.IGNORECASE):
            fallas.append(f"incluye lo prohibido /{patron}/")
    for patron in _FORMATO_PROHIBIDO:
        if re.search(patron, texto):
            fallas.append(f"formato no apto para WhatsApp /{patron}/")
    return fallas


# ── Juez (a ciegas: no sabe qué modelo escribió cada respuesta) ───────────────

class Veredicto(BaseModel):
    etiqueta: str
    cumple_criterios: bool
    cumple_voz: bool
    motivo: str


class Juicio(BaseModel):
    veredictos: list[Veredicto]


_INSTRUCCIONES_JUEZ = f"""Evalúas respuestas de un asistente de WhatsApp de Saboryarte (mezclas de sales y especias, Colombia).
Para cada respuesta das dos veredictos:
- «cumple_criterios»: cumple lo que el caso dice que «debe» hacer, no hace nada de lo que «no debe», y no afirma datos que
  contradigan o no estén en la base de conocimiento de abajo. Todo lo que aparece en la base es correcto: no lo marques como
  inventado. Pedir al cliente que escriba «menú» (para ver opciones o hacer un pedido guiado) o «asesor» (para hablar con una
  persona) son instrucciones válidas del asistente. Sí cuenta como falla prometer algo que el asistente no puede hacer (transferir, registrar, atender «de inmediato»).
- «cumple_voz»: tuteo (salvo que el caso sea un reclamo grave), extensión de WhatsApp (alrededor de 2 a 4 líneas; falla solo si
  es claramente larga), uno o dos emojis como máximo y una sola pregunta.
El motivo, en una frase corta en español; si ambas se cumplen, escribe «ok».

Reglas generales del asistente: {CASOS['reglas_generales']}

<base_de_conocimiento>
{_BASE_CONOCIMIENTO}
</base_de_conocimiento>"""


async def juzgar(caso: dict, respuestas: list[tuple[str, str]]) -> dict[str, Veredicto]:
    """respuestas: [(etiqueta, texto)], ya mezcladas para que el juez no sepa de qué modelo es cada una."""
    _verificar_presupuesto()
    reglas_emojis = "" if not caso.get("sin_reglas_de_emojis") else "\nEn este caso no debe usar emojis (reclamo grave)."
    bloques = "\n\n".join(f"<respuesta etiqueta=\"{etiqueta}\">\n{texto}\n</respuesta>" for etiqueta, texto in respuestas)
    contenido = (
        f"Mensaje del cliente: «{caso['mensaje']}»\n\nDebe: {caso['debe']}\nNo debe: {caso['no_debe']}{reglas_emojis}\n\n{bloques}"
    )
    respuesta = await client.messages.parse(
        model=JUEZ,
        max_tokens=16000,
        output_config={"effort": "low"},
        system=[{"type": "text", "text": _INSTRUCCIONES_JUEZ, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": contenido}],
        output_format=Juicio,
    )
    _registrar_costo(JUEZ, respuesta.usage)
    return {v.etiqueta: v for v in respuesta.parsed_output.veredictos}


# ── Ejecución ─────────────────────────────────────────────────────────────────

async def main(rejuzgar: Path | None = None) -> None:
    semaforo = asyncio.Semaphore(CONCURRENCIA)

    async def con_limite(coro):
        async with semaforo:
            return await coro

    casos = CASOS["casos"]
    resultados: dict[tuple[str, str, int], dict] = {}

    if rejuzgar:
        # Reutiliza las respuestas guardadas: solo se paga el juez.
        for x in json.loads(rejuzgar.read_text(encoding="utf-8"))["detalle"]:
            res = {k: v for k, v in x.items() if k not in ("modelo", "caso", "intento", "juez", "aprueba", "fallas_automaticas")}
            resultados[(x["modelo"], x["caso"], x["intento"])] = res
            if "costo_usd" in res:
                gasto["por_modelo"][x["modelo"]] = gasto["por_modelo"].get(x["modelo"], 0.0) + res["costo_usd"]
    else:
        # Primera llamada de cada modelo sola, para que escriba la caché del system prompt antes de leerla en paralelo.
        for modelo in MODELOS:
            resultados[(modelo, casos[0]["id"], 0)] = await responder(modelo, casos[0])
        pendientes = [(m, c, r) for m in MODELOS for c in casos for r in range(REPETICIONES) if (m, c["id"], r) not in resultados]
        salidas = await asyncio.gather(*(con_limite(responder(m, c)) for m, c, _ in pendientes), return_exceptions=True)
        for (m, c, r), salida in zip(pendientes, salidas):
            resultados[(m, c["id"], r)] = salida if isinstance(salida, dict) else {"error": repr(salida)}

    async def juzgar_caso(caso: dict) -> None:
        intentos = [(m, r, resultados[(m, caso["id"], r)]) for m in MODELOS for r in range(REPETICIONES)]
        validos = [(m, r, res) for m, r, res in intentos if "texto" in res and res["texto"].strip()]
        random.shuffle(validos)
        etiquetas = {f"R{i + 1}": (m, r) for i, (m, r, _) in enumerate(validos)}
        if not validos:
            return
        try:
            veredictos = await juzgar(caso, [(e, resultados[(m, caso["id"], r)]["texto"]) for e, (m, r) in etiquetas.items()])
        except Exception as e:  # presupuesto o error de la API: el caso queda sin juez
            for m, r in etiquetas.values():
                resultados[(m, caso["id"], r)]["juez"] = {"error": repr(e)}
            return
        for etiqueta, (m, r) in etiquetas.items():
            v = veredictos.get(etiqueta)
            resultados[(m, caso["id"], r)]["juez"] = (
                {"criterios": v.cumple_criterios, "voz": v.cumple_voz, "motivo": v.motivo} if v else {"error": "sin veredicto"}
            )

    # El primer juicio va solo para escribir la caché del prompt del juez; el resto la lee en paralelo.
    await juzgar_caso(casos[0])
    await asyncio.gather(*(con_limite(juzgar_caso(c)) for c in casos[1:]))

    # ── Resumen ──
    filas = []
    resumen = {m: {"aprobados": 0, "intentos": 0, "criticos_ok": 0, "criterios_ok": 0, "voz_ok": 0, "latencias": []} for m in MODELOS}
    for caso in casos:
        fila = {"id": caso["id"], "critico": caso["critico"]}
        for m in MODELOS:
            ok = 0
            for r in range(REPETICIONES):
                res = resultados[(m, caso["id"], r)]
                if "texto" not in res:
                    res["aprueba"] = False
                    continue
                res["fallas_automaticas"] = revisiones_automaticas(caso, res["texto"])
                juez = res.get("juez", {})
                criterios = not res["fallas_automaticas"] and juez.get("criterios") is True
                res["aprueba"] = criterios and juez.get("voz") is True
                ok += res["aprueba"]
                resumen[m]["criterios_ok"] += criterios
                resumen[m]["voz_ok"] += juez.get("voz") is True
                resumen[m]["latencias"].append(res["latencia_s"])
            fila[m] = ok
            resumen[m]["aprobados"] += ok
            resumen[m]["intentos"] += REPETICIONES
            if caso["critico"] and ok == REPETICIONES:
                resumen[m]["criticos_ok"] += 1
        filas.append(fila)

    criticos = sum(c["critico"] for c in casos)
    print(f"\n{'Caso':<9} {'Crítico':<8} " + " ".join(f"{m:<18}" for m in MODELOS))
    for fila in filas:
        print(f"{fila['id']:<9} {'sí' if fila['critico'] else '':<8} " + " ".join(f"{fila[m]}/{REPETICIONES:<16}" for m in MODELOS))
    print()
    for m in MODELOS:
        s = resumen[m]
        latencia = sum(s["latencias"]) / max(len(s["latencias"]), 1)
        print(
            f"{m}: {s['aprobados']}/{s['intentos']} aprobados (criterios {s['criterios_ok']}, voz {s['voz_ok']}) · "
            f"críticos 3/3: {s['criticos_ok']}/{criticos} · "
            f"latencia media {latencia:.1f} s · costo US${gasto['por_modelo'].get(m, 0):.3f}"
        )
    print(f"Juez ({JUEZ}): US${gasto['por_modelo'].get(JUEZ, 0):.3f} · Total: US${gasto['total']:.3f}")

    salida = RAIZ / "resultados" / f"comparacion-{datetime.now():%Y%m%d-%H%M%S}.json"
    salida.parent.mkdir(exist_ok=True)
    salida.write_text(json.dumps({
        "modelos": MODELOS, "juez": JUEZ, "repeticiones": REPETICIONES, "esfuerzo": ESFUERZO,
        "gasto_usd": gasto, "resumen_por_caso": filas,
        "detalle": [{"modelo": m, "caso": c, "intento": r, **res} for (m, c, r), res in resultados.items()],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Detalle: {salida.relative_to(RAIZ.parent)}")


if __name__ == "__main__":
    argumentos = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    if "--modelos" in argumentos:
        MODELOS = argumentos["--modelos"].split(",")
    archivo = Path(argumentos["--rejuzgar"]) if "--rejuzgar" in argumentos else None
    asyncio.run(main(archivo))
