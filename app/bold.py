"""Cliente de Bold: links de pago (API Link de pagos) y verificación de los avisos del webhook.

Documentación: https://developers.bold.co/pagos-en-linea/api-link-de-pagos y https://developers.bold.co/webhook
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import time

import httpx
from httpx import HTTPError

from .config import settings

logger = logging.getLogger(__name__)

_BOLD_API_URL = "https://integrations.api.bold.co"


def is_enabled() -> bool:
    return bool(settings.bold_api_key)


def _headers() -> dict[str, str]:
    # Bold autentica con la llave de identidad en este formato (no «Bearer»).
    return {"Authorization": f"x-api-key {settings.bold_api_key}", "Content-Type": "application/json"}


async def create_payment_link(total: int, description: str, reference: str) -> dict | None:
    """Crea un link de monto cerrado en COP. Devuelve {"id", "url", "vence_en"} o None si Bold no lo creó.

    Sin «payment_methods»: el link ofrece todos los métodos habilitados (tarjeta, PSE, Botón Bancolombia, Nequi).
    """
    expires_at = time.time() + settings.bold_link_hours * 3600
    body = {
        "amount_type": "CLOSE",
        "amount": {"currency": "COP", "total_amount": total, "tip_amount": 0},
        "reference": reference,
        "description": description,
        # Nanosegundos Unix. Los ejemplos de la documentación usan milisegundos, pero Bold rechaza ese valor
        # como fecha pasada (verificado en el ambiente de pruebas el 28 oct 2026).
        "expiration_date": int(expires_at) * 1_000_000_000,
    }
    if settings.bold_return_url:
        body["callback_url"] = settings.bold_return_url
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{_BOLD_API_URL}/online/link/v1", headers=_headers(), json=body)
        response.raise_for_status()
        data = response.json()
    except (HTTPError, ValueError) as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        logger.error("Bold: no se pudo crear el link de pago (%s, HTTP %s)", type(e).__name__, status_code)
        return None
    payload = data.get("payload") or {}
    if data.get("errors") or not payload.get("url") or not payload.get("payment_link"):
        logger.error("Bold: respuesta sin link de pago (errores: %s)", data.get("errors"))
        return None
    return {"id": payload["payment_link"], "url": payload["url"], "vence_en": expires_at}


async def link_status(link_id: str) -> str | None:
    """Estado del link según Bold (ACTIVE, PROCESSING, PAID, REJECTED, CANCELLED, EXPIRED) o None si no respondió."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{_BOLD_API_URL}/online/link/v1/{link_id}", headers=_headers())
        response.raise_for_status()
        data = response.json()
    except (HTTPError, ValueError) as e:
        logger.error("Bold: no se pudo consultar el link (%s)", type(e).__name__)
        return None
    info = data.get("payload") or data
    return info.get("status")


async def confirm_paid(link_id: str, attempts: int = 4, wait_seconds: float = 15) -> str | None:
    """Consulta el link hasta verlo PAID. El aviso puede llegar antes de que Bold actualice el link (PROCESSING),
    así que se reintenta unos segundos antes de decidir. Devuelve el último estado visto."""
    status = None
    for attempt in range(attempts):
        status = await link_status(link_id)
        if status != "PROCESSING":
            return status
        if attempt < attempts - 1:
            await asyncio.sleep(wait_seconds)
    return status


def valid_signature(body: bytes, signature: str | None) -> bool:
    """Verifica «x-bold-signature»: HMAC-SHA256 (hex) del cuerpo en Base64 con la llave secreta.

    En el ambiente de pruebas Bold firma con una llave vacía. En producción la llave secreta es obligatoria:
    sin ella cualquiera podría enviar un «pago aprobado» falso.
    """
    if not signature:
        return False
    if settings.bold_sandbox:
        key = ""
    elif settings.bold_secret_key:
        key = settings.bold_secret_key
    else:
        logger.error("Bold: falta BOLD_SECRET_KEY; se rechazan los avisos de pago")
        return False
    expected = hmac.new(key.encode(), base64.b64encode(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
