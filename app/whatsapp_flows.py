"""Administra el WhatsApp Flow del kit armado (formulario con casillas para elegir las mezclas).

Uso:
  .venv/bin/python -m app.whatsapp_flows crear <WABA_ID>   crea el Flow y sube app/flows/kit_flow.json (queda en borrador)
  .venv/bin/python -m app.whatsapp_flows subir             vuelve a subir el JSON al Flow de WHATSAPP_KIT_FLOW_ID
  .venv/bin/python -m app.whatsapp_flows publicar          publica el Flow (después ya no se puede editar)

El WABA_ID aparece en Meta for Developers → WhatsApp → API Setup, debajo del ID del número.
"""
import json
import sys
from pathlib import Path

import httpx

from .config import settings

_GRAPH = "https://graph.facebook.com/v21.0"
KIT_FLOW_JSON = Path(__file__).with_name("flows") / "kit_flow.json"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.whatsapp_token}"}


def create_flow(waba_id: str) -> str:
    response = httpx.post(
        f"{_GRAPH}/{waba_id}/flows", headers=_headers(), timeout=30,
        data={"name": "chatsabor_kit_mezclas", "categories": json.dumps(["OTHER"])},
    )
    response.raise_for_status()
    return response.json()["id"]


def upload_flow_json(flow_id: str) -> list[dict]:
    """Sube el JSON del Flow. Devuelve los errores de validación de Meta (lista vacía si está bien)."""
    response = httpx.post(
        f"{_GRAPH}/{flow_id}/assets", headers=_headers(), timeout=30,
        data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        files={"file": ("flow.json", KIT_FLOW_JSON.read_bytes(), "application/json")},
    )
    response.raise_for_status()
    return response.json().get("validation_errors", [])


def publish_flow(flow_id: str) -> None:
    httpx.post(f"{_GRAPH}/{flow_id}/publish", headers=_headers(), timeout=30).raise_for_status()


def _main(args: list[str]) -> None:
    command = args[0] if args else ""
    if command == "crear" and len(args) == 2:
        flow_id = create_flow(args[1])
        print(f"Flow creado: {flow_id}")
    elif command in ("subir", "publicar") and settings.whatsapp_kit_flow_id:
        flow_id = settings.whatsapp_kit_flow_id
    else:
        print(__doc__)
        sys.exit(1)
    if command in ("crear", "subir"):
        errors = upload_flow_json(flow_id)
        print("JSON subido sin errores" if not errors else f"Errores de validación: {json.dumps(errors, ensure_ascii=False, indent=2)}")
        if command == "crear":
            print(f"Agrega al .env:  WHATSAPP_KIT_FLOW_ID={flow_id}")
    elif command == "publicar":
        publish_flow(flow_id)
        print("Flow publicado. Cambia WHATSAPP_FLOWS_MODE=published en el .env.")


if __name__ == "__main__":
    _main(sys.argv[1:])
