"""Avisos al equipo por correo (fichas, pagos, alertas y exportes). Usa SMTP de la librería estándar."""
import asyncio
import html
import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)


def _html(body: str, buttons: list[tuple[str, str]]) -> str:
    """Versión HTML del aviso: el mismo texto y botones grandes para tocar desde el celular."""
    links = "".join(
        f'<a href="{html.escape(url)}" style="display:inline-block;margin:6px 8px 6px 0;padding:12px 20px;'
        f'border-radius:8px;background:{"#5B7F3A" if i == 0 else "#9A3B2E"};color:#fff;text-decoration:none;'
        f'font:600 15px sans-serif">{html.escape(label)}</a>'
        for i, (label, url) in enumerate(buttons)
    )
    text = html.escape(body).replace("\n", "<br>")
    return f'<div style="font:15px/1.5 sans-serif;color:#222">{text}<p>{links}</p></div>'


def _send_email(subject: str, body: str, attachments: list[Path], buttons: list[tuple[str, str]]) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = settings.handoff_email_to
    plain = body + ("\n\n" + "\n".join(f"{label}: {url}" for label, url in buttons) if buttons else "")
    message.set_content(plain)
    if buttons:
        message.add_alternative(_html(body, buttons), subtype="html")
    for path in attachments:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


async def notify_team(subject: str, body: str, attachments: list[Path] | None = None,
                      buttons: list[tuple[str, str]] | None = None) -> bool:
    """Envía el aviso al correo del equipo. Devuelve False si no está configurado o falló (el bot sigue igual).

    attachments: archivos adjuntos (comprobantes, exportes). buttons: (texto, url) que se ven como botones.
    """
    if not (settings.handoff_email_to and settings.smtp_host and settings.smtp_user and settings.smtp_password):
        logger.warning("Aviso al equipo no enviado: falta configurar HANDOFF_EMAIL_TO o SMTP_*")
        return False
    try:
        await asyncio.to_thread(_send_email, subject, body, attachments or [], buttons or [])
    except (smtplib.SMTPException, OSError) as e:
        logger.error("Aviso al equipo no enviado: %s", type(e).__name__)
        return False
    logger.info("Aviso al equipo enviado")
    return True
