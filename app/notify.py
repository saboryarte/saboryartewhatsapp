"""Avisos al equipo por correo (ficha de traspaso y alertas). Usa SMTP de la librería estándar."""
import asyncio
import logging
import smtplib
from email.message import EmailMessage

from .config import settings

logger = logging.getLogger(__name__)


def _send_email(subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = settings.handoff_email_to
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


async def notify_team(subject: str, body: str) -> bool:
    """Envía el aviso al correo del equipo. Devuelve False si no está configurado o falló (el bot sigue igual)."""
    if not (settings.handoff_email_to and settings.smtp_host and settings.smtp_user and settings.smtp_password):
        logger.warning("Aviso al equipo no enviado: falta configurar HANDOFF_EMAIL_TO o SMTP_*")
        return False
    try:
        await asyncio.to_thread(_send_email, subject, body)
    except (smtplib.SMTPException, OSError) as e:
        logger.error("Aviso al equipo no enviado: %s", type(e).__name__)
        return False
    logger.info("Aviso al equipo enviado")
    return True
