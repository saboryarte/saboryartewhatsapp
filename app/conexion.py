"""Conexión del número de WhatsApp de Saboryarte al bot en coexistencia: el equipo sigue usando la app de
WhatsApp Business en el celular y Especia responde por la API en el mismo número.

Uso (en el servidor):
    .venv/bin/python -m app.conexion enlace        crea el enlace (válido 48 h) de la página de conexión
    .venv/bin/python -m app.conexion estado        qué número usa el bot, si sigue en coexistencia y si la llave vence
    .venv/bin/python -m app.conexion llave         pasa a la llave permanente del .env (después de darle acceso)
    .venv/bin/python -m app.conexion sincronizar   vuelve a pedir contactos e historial (solo en las primeras 24 h)

La llave que entrega Meta al conectar vence a los 60 días (configuración «WhatsApp Embedded Signup Configuration
With 60 Expiration Token»). Por eso, si el usuario del sistema del .env (WHATSAPP_TOKEN, no vence) tiene acceso a la
cuenta de WhatsApp conectada, el bot usa esa llave. Si no, usa la de 60 días y avisa al equipo cómo darle acceso;
cuando falten AVISO_DIAS días para el vencimiento, lo recuerda por correo cada día.

Cómo funciona (guía completa en docs/despliegue/numero-del-negocio.md):
1. La dueña abre el enlace, toca «Conectar», inicia sesión en Facebook y escanea con la app de WhatsApp Business el
   código QR que muestra Meta («Embedded Signup» con featureType «whatsapp_business_app_onboarding»).
2. Meta le entrega a la página un código de un solo uso (vence en 30 s) que el servidor cambia por la llave de
   acceso del negocio. El bot suscribe su app a la cuenta de WhatsApp, guarda el número y la llave en
   data/whatsapp_conexion.json y desde ese momento responde en el número del negocio (sin reiniciar).
3. Se pide a Meta la sincronización de contactos y del historial (Meta solo la permite una vez y en las primeras 24 h).
   El historial no se copia al archivo de chats: solo se registra que llegó.

Desde ese momento todo cliente que escriba al número recibe respuesta de Especia: conectar es salir al aire.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

import httpx

from . import config
from .config import conexion_path, settings
from .handoff import COLOMBIA
from .notify import notify_team

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v24.0"
_GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
FEATURE_TYPE = "whatsapp_business_app_onboarding"
LINK_HOURS = 48
# Días antes del vencimiento de la llave de 60 días en que el bot empieza a avisar al equipo.
AVISO_DIAS = 10

# Motivos de desconexión que Meta envía en account_update (PARTNER_REMOVED), en palabras del equipo.
MOTIVOS_DESCONEXION = {
    "PRIMARY_INACTIVITY": "el celular con la app de WhatsApp Business no se abrió en unos 14 días",
    "COMPANION_INACTIVITY": "un dispositivo vinculado lleva unos 30 días sin usarse",
    "ACCOUNT_DISCONNECTED": "alguien desconectó la cuenta desde la app (Ajustes → Cuenta → Plataforma para empresas)",
    "BUSINESS_DOWNGRADE": "la cuenta dejó de ser de WhatsApp Business",
    "CHANGE_NUMBER": "se cambió el número en la app",
    "USER_RE_REGISTERED": "el número se volvió a registrar en la app",
}
_EVENTOS_DESCONEXION = ("PARTNER_REMOVED", "ACCOUNT_OFFBOARDED")


class ConexionError(Exception):
    """Fallo de la conexión, con un mensaje para mostrar en la página."""


# ── Enlace firmado de la página de conexión ──────────────────────────────────

def _firma(exp: int) -> str:
    return hmac.new(settings.transfer_link_secret.encode(), f"conexion:{exp}".encode(), hashlib.sha256).hexdigest()


def signup_link(hours: int = LINK_HOURS) -> str:
    if not (settings.public_base_url and settings.transfer_link_secret):
        raise ConexionError("Faltan PUBLIC_BASE_URL y TRANSFER_LINK_SECRET en el .env.")
    exp = int(time.time()) + hours * 3600
    return f"{settings.public_base_url.rstrip('/')}/conectar?exp={exp}&t={_firma(exp)}"


def valid_link(exp: int, token: str) -> bool:
    return (bool(settings.transfer_link_secret) and exp > time.time()
            and hmac.compare_digest(_firma(exp), token))


def signup_page(exp: int, token: str) -> str:
    """Cuerpo de la página: botón «Conectar» con el SDK de Facebook (Embedded Signup en coexistencia)."""
    if not (settings.meta_app_id and settings.meta_signup_config_id):
        return "<p>Falta configurar META_APP_ID y META_SIGNUP_CONFIG_ID en el servidor.</p>"
    datos = json.dumps({"appId": settings.meta_app_id, "configId": settings.meta_signup_config_id,
                        "version": GRAPH_VERSION, "featureType": FEATURE_TYPE, "exp": exp, "t": token},
                       ).replace("<", "\\u003c")  # dentro de <script>: que ningún valor pueda cerrar la etiqueta
    return f"""
<p>Vas a conectar el WhatsApp de Saboryarte con Especia. Ten a mano el celular con la app de
<b>WhatsApp Business</b>: Meta te mostrará un código QR para escanearlo desde la app.</p>
<p>Seguirás usando la app como siempre. Especia responde a los clientes y tú ves todo en el celular.</p>
<p><button class="ok" id="conectar" onclick="conectar()">Conectar WhatsApp</button></p>
<p id="estado" role="status"></p>
<script>
const D = {datos};
let sesion = {{}};
function estado(texto) {{ document.getElementById('estado').textContent = texto; }}
window.fbAsyncInit = function () {{
  FB.init({{appId: D.appId, autoLogAppEvents: true, xfbml: true, version: D.version}});
}};
window.addEventListener('message', function (event) {{
  if (!event.origin.endsWith('facebook.com')) return;
  try {{
    const d = JSON.parse(event.data);
    if (d.type !== 'WA_EMBEDDED_SIGNUP') return;
    if (d.event === 'CANCEL') estado('Cancelaste la conexión. Puedes intentarlo de nuevo.');
    else if (d.event === 'ERROR') estado('Meta mostró un error: ' + (d.data && d.data.error_message || 'sin detalle'));
    else sesion = d.data || {{}};
  }} catch (e) {{}}
}});
function enviar(code) {{
  estado('Conectando… no cierres esta página.');
  const waba = sesion.waba_id || (sesion.messaging_account_ids || [])[0] || '';
  fetch('/conectar', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{code: code, waba_id: waba, phone_number_id: sesion.phone_number_id || '', exp: D.exp, t: D.t}})}})
    .then(function (r) {{ return r.json(); }})
    .then(function (r) {{ estado(r.mensaje); if (r.ok) document.getElementById('conectar').hidden = true; }})
    .catch(function () {{ estado('No se pudo hablar con el servidor. Intenta de nuevo.'); }});
}}
function conectar() {{
  // El SDK no acepta funciones async como callback.
  FB.login(function (response) {{
    const code = response.authResponse && response.authResponse.code;
    if (!code) {{ estado('No se completó la conexión. Puedes intentarlo de nuevo.'); return; }}
    // La información de la sesión (cuenta y número) llega por otro mensaje; se espera un momento.
    setTimeout(function () {{ enviar(code); }}, 1500);
  }}, {{config_id: D.configId, response_type: 'code', override_default_response_type: true,
       extras: {{setup: {{}}, featureType: D.featureType, sessionInfoVersion: '3'}}}});
}}
</script>
<script async defer crossorigin="anonymous" src="https://connect.facebook.net/es_LA/sdk.js"></script>
"""


# ── Llamadas a Meta ───────────────────────────────────────────────────────────

async def _graph(method: str, path: str, token: str | None = None, **kwargs) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, f"{_GRAPH}/{path}", headers=headers, **kwargs)
    data = response.json() if response.content else {}
    if response.is_error:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise ConexionError(f"Meta respondió {response.status_code} en {path.split('?')[0]}: "
                            f"{error.get('message', 'sin detalle')} (código {error.get('code', '?')})")
    return data


async def _exchange_code(code: str) -> str:
    data = await _graph("GET", "oauth/access_token", params={
        "client_id": settings.meta_app_id, "client_secret": settings.app_secret, "code": code})
    if not data.get("access_token"):
        raise ConexionError("Meta no entregó la llave de acceso del negocio.")
    return data["access_token"]


async def _waba_from_token(token: str) -> str:
    """Cuenta de WhatsApp (WABA) a la que da acceso la llave, por si la página no la recibió."""
    data = await _graph("GET", "debug_token", params={
        "input_token": token, "access_token": f"{settings.meta_app_id}|{settings.app_secret}"})
    for scope in data.get("data", {}).get("granular_scopes", []):
        if scope.get("scope") == "whatsapp_business_management" and scope.get("target_ids"):
            return scope["target_ids"][0]
    raise ConexionError("No se encontró la cuenta de WhatsApp del negocio en la llave de acceso.")


async def _pick_number(waba_id: str, token: str, phone_number_id: str) -> dict[str, Any]:
    data = await _graph("GET", f"{waba_id}/phone_numbers", token, params={
        "fields": "id,display_phone_number,verified_name,is_on_biz_app,platform_type"})
    numeros = data.get("data", [])
    elegido = (next((n for n in numeros if n["id"] == phone_number_id), None)
               or next((n for n in numeros if n.get("is_on_biz_app")), None)
               or (numeros[0] if len(numeros) == 1 else None))
    if elegido is None:
        raise ConexionError("La cuenta de WhatsApp no tiene un número para conectar.")
    return elegido


async def sync_app_data(phone_number_id: str, token: str) -> dict[str, str]:
    """Pide la sincronización de contactos y del historial. Meta solo la permite una vez y en las primeras 24 h."""
    resultado = {}
    for nombre, sync_type in (("contactos", "smb_app_state_sync"), ("historial", "history")):
        try:
            data = await _graph("POST", f"{phone_number_id}/smb_app_data", token,
                                json={"messaging_product": "whatsapp", "sync_type": sync_type})
            resultado[nombre] = f"pedida (request_id {data.get('request_id', '?')})"
        except (ConexionError, httpx.HTTPError) as e:
            resultado[nombre] = f"falló: {e}"
            logger.error("Sincronización de %s no pedida: %s", nombre, e)
    return resultado


async def _permanent_token_for(waba_id: str) -> str | None:
    """La llave del .env (usuario del sistema, no vence), si ya tiene acceso a la cuenta de WhatsApp conectada."""
    token = config.env_whatsapp_token
    if not token:
        return None
    try:
        await _graph("GET", f"{waba_id}/phone_numbers", token, params={"fields": "id"})
    except (ConexionError, httpx.HTTPError):
        return None
    return token


async def _token_expiry(token: str) -> float | None:
    """Cuándo vence la llave (timestamp). None si no vence o si Meta no lo dice."""
    try:
        data = await _graph("GET", "debug_token", params={
            "input_token": token, "access_token": f"{settings.meta_app_id}|{settings.app_secret}"})
    except (ConexionError, httpx.HTTPError) as e:
        logger.warning("No se pudo consultar el vencimiento de la llave: %s", e)
        return None
    expira = data.get("data", {}).get("expires_at")
    return float(expira) if expira else None


def _fecha(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=COLOMBIA).strftime("%Y-%m-%d")


def _instrucciones_llave(datos: dict[str, Any]) -> str:
    return (f"Para que no venza: en business.facebook.com → Configuración → Usuarios del sistema, elige el usuario del "
            f"bot → Asignar activos → Cuentas de WhatsApp → «{datos.get('nombre') or datos.get('numero')}» → control "
            f"total. Después, en el servidor: .venv/bin/python -m app.conexion llave")


def _save(datos: dict[str, Any]) -> None:
    path = conexion_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Contiene la llave de acceso: solo la puede leer el usuario del bot.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


async def connect(code: str, waba_id: str = "", phone_number_id: str = "") -> str:
    """Termina la conexión iniciada en la página. Devuelve el mensaje para la dueña."""
    if not (settings.meta_app_id and settings.app_secret):
        raise ConexionError("Faltan META_APP_ID o APP_SECRET en el servidor.")
    token = await _exchange_code(code)
    waba_id = waba_id or await _waba_from_token(token)
    numero = await _pick_number(waba_id, token, phone_number_id)
    await _graph("POST", f"{waba_id}/subscribed_apps", token)
    # El bot usa la llave permanente del .env si ya tiene acceso a la cuenta; si no, la de la conexión (60 días).
    permanente = await _permanent_token_for(waba_id)
    datos = {
        "waba_id": waba_id, "phone_number_id": numero["id"],
        "numero": numero.get("display_phone_number", ""), "nombre": numero.get("verified_name", ""),
        "en_app": bool(numero.get("is_on_biz_app")), "conectado_en": time.time(),
        "token": permanente or token, "llave": "permanente" if permanente else "conexion",
        "token_vence": None if permanente else await _token_expiry(token),
    }
    _save(datos)
    # Desde ya el bot responde en el número del negocio (al reiniciar lo toma de data/whatsapp_conexion.json).
    settings.whatsapp_token, settings.whatsapp_phone_number_id = datos["token"], numero["id"]
    datos["sincronizacion"] = await sync_app_data(numero["id"], token)
    _save(datos)
    logger.info("Número %s conectado (WABA %s, coexistencia: %s)", datos["numero"], waba_id, datos["en_app"])
    aviso = ("" if datos["en_app"] else "\n\nOjo: Meta no reporta este número como activo en la app de WhatsApp "
             "Business (coexistencia). El equipo no podrá atender desde la app: revisa la guía de conexión.")
    if not permanente:
        vence = f"el {_fecha(datos['token_vence'])}" if datos["token_vence"] else "en unos 60 días"
        aviso += (f"\n\nImportante: la llave de esta conexión vence {vence}; ese día Especia dejaría de responder. "
                  + _instrucciones_llave(datos))
    await notify_team(
        f"[Saboryarte] ✅ WhatsApp {datos['numero']} conectado a Especia",
        f"El número {datos['numero']} ({datos['nombre']}) quedó conectado. Desde ahora Especia responde a los "
        f"clientes y el equipo sigue atendiendo desde la app de WhatsApp Business.\n\n"
        f"Contactos: {datos['sincronizacion']['contactos']}\nHistorial: {datos['sincronizacion']['historial']}{aviso}",
    )
    return (f"¡Listo! El WhatsApp {datos['numero']} quedó conectado con Especia. Desde ahora Especia responde a "
            "los clientes y tú sigues viendo y atendiendo todo desde la app.")


def _load() -> dict[str, Any] | None:
    path = conexion_path()
    return json.loads(path.read_text()) if path.exists() else None


async def use_permanent_token() -> str:
    """Pasa el número conectado a la llave permanente del .env, si ya tiene acceso a la cuenta."""
    datos = _load()
    if datos is None:
        raise ConexionError("No hay un número del negocio conectado.")
    if datos.get("llave") == "permanente":
        return "El bot ya usa la llave permanente."
    token = await _permanent_token_for(datos["waba_id"])
    if token is None:
        raise ConexionError("La llave del .env todavía no tiene acceso a la cuenta de WhatsApp. " + _instrucciones_llave(datos))
    datos.update(token=token, llave="permanente", token_vence=None)
    _save(datos)
    settings.whatsapp_token = token
    return "Listo: el bot usa la llave permanente; ya no hay vencimiento."


async def check_token_expiry(now: float | None = None) -> bool:
    """Se llama una vez al día. Avisa al equipo si la llave de 60 días vence en AVISO_DIAS días o menos."""
    datos = _load()
    if not datos or not datos.get("token_vence"):
        return False
    dias = (datos["token_vence"] - (now or time.time())) / 86400
    if dias > AVISO_DIAS:
        return False
    estado = (f"vence en {max(int(dias), 0)} día(s), el {_fecha(datos['token_vence'])}" if dias > 0
              else f"venció el {_fecha(datos['token_vence'])}: Especia ya no puede responder")
    await notify_team("[Saboryarte] ⏳ La llave de WhatsApp de Especia vence pronto",
                      f"La llave con la que Especia responde en {datos.get('numero')} {estado}.\n\n"
                      + _instrucciones_llave(datos))
    return True


# ── Avisos de Meta sobre la conexión (webhook) ───────────────────────────────

async def handle_event(field: str, value: dict[str, Any]) -> None:
    """account_update (desconexión), history y smb_app_state_sync."""
    if field == "account_update":
        evento = value.get("event", "")
        if evento in _EVENTOS_DESCONEXION:
            info = value.get("disconnection_info") or {}
            motivo = MOTIVOS_DESCONEXION.get(info.get("reason", ""), info.get("reason") or "Meta no dio el motivo")
            logger.error("WhatsApp desconectado del bot (%s): %s", evento, motivo)
            await notify_team(
                "[Saboryarte] 🚨 Especia quedó desconectado del WhatsApp",
                f"Meta avisó que el número se desconectó del bot ({evento}). Motivo: {motivo}.\n\n"
                "Los clientes siguen escribiendo a la app de WhatsApp Business, pero Especia ya no les responde: "
                "atiéndelos desde la app mientras tanto.\n\nPara volver a conectarlo, pide un enlace nuevo "
                "(python -m app.conexion enlace) y repite la conexión.",
            )
        else:
            logger.info("Aviso de cuenta de Meta: %s", evento or value)
    elif field == "history":
        for bloque in value.get("history") or []:
            meta = bloque.get("metadata") or {}
            logger.info("Historial de la app: fase %s, parte %s, avance %s%% (%d chats)", meta.get("phase"),
                        meta.get("chunk_order"), meta.get("progress"), len(bloque.get("threads") or []))
        for error in value.get("errors") or []:
            logger.warning("Historial no sincronizado: %s (código %s)", error.get("title"), error.get("code"))
    elif field == "smb_app_state_sync":
        logger.info("Contactos de la app sincronizados: %d", len(value.get("state_sync") or []))


# ── Línea de comandos ────────────────────────────────────────────────────────

def _mask(token: str) -> str:
    return f"…{token[-4:]} ({len(token)} caracteres)" if token else "(vacía)"


async def _status() -> None:
    path = conexion_path()
    if not path.exists():
        print(f"Sin número del negocio conectado: el bot usa el número del .env ({settings.whatsapp_phone_number_id}).")
        return
    datos = json.loads(path.read_text())
    print(f"Número conectado: {datos.get('numero')} ({datos.get('nombre')}), WABA {datos.get('waba_id')}")
    vence = datos.get("token_vence")
    print(f"Llave: {_mask(datos.get('token', ''))} · "
          + ("permanente (no vence)" if datos.get("llave") == "permanente"
             else f"de la conexión, vence el {_fecha(vence)}" if vence else "de la conexión (vencimiento desconocido)"))
    print(f"Sincronización: {datos.get('sincronizacion', 'sin pedir')}")
    try:
        numero = await _graph("GET", datos["phone_number_id"], datos["token"], params={
            "fields": "display_phone_number,verified_name,is_on_biz_app,platform_type,quality_rating,"
                      "messaging_limit_tier"})
        apps = await _graph("GET", f"{datos['waba_id']}/subscribed_apps", datos["token"])
    except (ConexionError, httpx.HTTPError) as e:
        print(f"No se pudo consultar a Meta: {e}")
        return
    print(f"Meta dice: en la app (coexistencia) = {numero.get('is_on_biz_app')}, plataforma = "
          f"{numero.get('platform_type')}, calidad = {numero.get('quality_rating')}, "
          f"límite = {numero.get('messaging_limit_tier')}")
    print(f"Apps suscritas a la cuenta: {len(apps.get('data', []))}")


async def _resync() -> None:
    datos = json.loads(conexion_path().read_text())
    datos["sincronizacion"] = await sync_app_data(datos["phone_number_id"], datos["token"])
    _save(datos)
    print(datos["sincronizacion"])


def main() -> None:
    logging.basicConfig(level=settings.log_level)
    parser = argparse.ArgumentParser(description="Conexión del número de WhatsApp de Saboryarte (coexistencia)")
    parser.add_argument("accion", choices=["enlace", "estado", "llave", "sincronizar"])
    args = parser.parse_args()
    try:
        if args.accion == "enlace":
            print(signup_link())
            print(f"Válido por {LINK_HOURS} horas. Ábrelo en un computador con el celular de la app a la mano.")
        elif args.accion == "estado":
            asyncio.run(_status())
        elif args.accion == "llave":
            print(asyncio.run(use_permanent_token()))
        else:
            asyncio.run(_resync())
    except ConexionError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
