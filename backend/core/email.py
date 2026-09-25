"""Entrega de enlaces de verificación de email."""

import asyncio
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

from backend.core.config import settings


class EmailDeliveryError(RuntimeError):
    """Error controlado cuando no se puede entregar un email."""


def _send_smtp(message: EmailMessage) -> None:
    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.smtp_timeout_seconds,
    ) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(
                settings.smtp_username.get_secret_value(),
                settings.smtp_password.get_secret_value(),
            )
        smtp.send_message(message)


async def deliver_email_verification(email: str, token: str) -> None:
    """Entrega el enlace de verificación; en desarrollo solo habilita el token local."""

    if settings.email_verification_delivery_mode == "development":
        return

    verification_url = (
        f"{settings.frontend_base_url.rstrip('/')}/verify-email?token={quote(token, safe='')}"
    )
    message = EmailMessage()
    message["Subject"] = "Verify your Mind Guard Fenix Team email"
    message["From"] = str(settings.email_verification_from)
    message["To"] = email
    message.set_content(
        "Verify your email to activate your Mind Guard Fenix Team account:\n\n"
        f"{verification_url}\n\n"
        "If you did not request this account, you can ignore this message."
    )

    try:
        await asyncio.to_thread(_send_smtp, message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailDeliveryError("No se pudo entregar el email de verificación") from error
