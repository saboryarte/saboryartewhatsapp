import json
import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    verify_token: str
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    app_secret: str = ""
    anthropic_api_key: str = ""

    # Archivo SQLite con el estado de las conversaciones y los ids de mensajes ya procesados.
    state_db_path: str = "data/conversaciones.db"

    # Copias de seguridad (app/respaldo.py): remotos de rclone. Vacíos = la copia queda solo en el servidor.
    # RESPALDO_REMOTO: remoto «crypt» sobre Google Drive (cifra la base). PEDIDOS_REMOTO: carpeta legible del equipo.
    respaldo_remoto: str = ""
    pedidos_remoto: str = ""

    # Traspaso a una persona: correo que recibe la ficha y servidor SMTP para enviarlo.
    handoff_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Minutos (en horario de atención) sin respuesta del equipo antes de enviar una alerta.
    handoff_alert_minutes: int = 60
    # Horas desde el último mensaje del equipo tras las cuales el bot vuelve a responder solo.
    handoff_idle_hours: int = 12

    # Bold: links de pago. Llave de identidad (crea links) y llave secreta (verifica los avisos del webhook).
    bold_api_key: str = ""
    bold_secret_key: str = ""
    # true solo con las llaves de pruebas: Bold firma los avisos de prueba con una llave vacía.
    bold_sandbox: bool = False
    # Vigencia del link de pago (24 h, como la ventana para escribirle al cliente sin plantilla).
    bold_link_hours: int = 24
    # A dónde lleva el botón «Volver a la tienda» del comprobante de Bold (https). Lo ideal: el chat de WhatsApp,
    # https://wa.me/<número del bot sin + ni espacios>.
    bold_return_url: str = ""

    # Dirección pública del bot (https, sin / al final) y llave para firmar los enlaces del equipo: los botones
    # «Confirmar pago» / «No llegó» del correo del comprobante y el enlace de conexión del número (app/conexion.py).
    public_base_url: str = ""
    transfer_link_secret: str = ""

    # Conexión del número del negocio en coexistencia (app/conexion.py): ID de la app de Meta y ID de la
    # configuración de «Embedded Signup» (Meta for Developers → Facebook Login for Business → Configuraciones).
    meta_app_id: str = ""
    meta_signup_config_id: str = ""

    # WhatsApp Flow del kit armado (casillas para elegir las mezclas). Sin ID se usa la lista de una en una.
    whatsapp_kit_flow_id: str = ""
    # «draft» mientras se prueba (solo la versión borrador); «published» después de publicarlo.
    whatsapp_flows_mode: str = "draft"

    log_level: str = "INFO"


settings = Settings()

# Llave del .env (usuario del sistema, no vence). Se guarda antes de aplicar la conexión: app/conexion.py la usa
# para el número del negocio si ese usuario del sistema tiene acceso a la nueva cuenta de WhatsApp.
env_whatsapp_token = settings.whatsapp_token


def conexion_path() -> Path:
    """Número del negocio conectado en coexistencia (lo escribe app/conexion.py; contiene la llave de acceso)."""
    return Path(settings.state_db_path).parent / "whatsapp_conexion.json"


def apply_connection() -> dict | None:
    """Si el número del negocio ya se conectó, el bot usa ese número y su llave en vez de los del .env.

    Para volver al número de prueba: borrar data/whatsapp_conexion.json y reiniciar el bot.
    """
    path = conexion_path()
    if not path.exists():
        return None
    try:
        datos = json.loads(path.read_text())
        settings.whatsapp_token = datos["token"]
        settings.whatsapp_phone_number_id = datos["phone_number_id"]
    except (OSError, ValueError, KeyError):
        logging.getLogger(__name__).error("No se pudo leer %s: el bot sigue con el número del .env", path)
        return None
    return datos


apply_connection()
