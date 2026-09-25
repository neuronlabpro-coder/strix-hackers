"""Entrega de enlaces de verificación e invitaciones."""

import asyncio
import smtplib
import ssl
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
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username and settings.smtp_password:
            smtp.login(
                settings.smtp_username.get_secret_value(),
                settings.smtp_password.get_secret_value(),
            )
        smtp.send_message(message)


def _frontend_link(path: str, token: str) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}{path}?token={quote(token, safe='')}"


async def _deliver_message(message: EmailMessage) -> None:
    try:
        await asyncio.to_thread(_send_smtp, message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailDeliveryError("No se pudo entregar el email") from error


async def deliver_email_verification(email: str, token: str) -> None:
    """Entrega el enlace de verificación; en desarrollo no contacta SMTP."""

    if settings.email_verification_delivery_mode == "development":
        return

    message = EmailMessage()
    message["Subject"] = "Verify your Mind Guard Fenix Team email"
    message["From"] = str(settings.email_verification_from)
    message["To"] = email
    message.set_content(
        "Verify your email to activate your Mind Guard Fenix Team account:\n\n"
        f"{_frontend_link('/verify-email', token)}\n\n"
        "If you did not request this account, you can ignore this message."
    )
    await _deliver_message(message)


async def deliver_invitation(email: str, token: str) -> None:
    """Entrega el enlace de aceptación de una organización."""

    if settings.email_verification_delivery_mode == "development":
        return

    message = EmailMessage()
    message["Subject"] = "Invitation to a Mind Guard Fenix Team workspace"
    message["From"] = str(settings.email_verification_from)
    message["To"] = email
    message.set_content(
        "You have been invited to join a Mind Guard Fenix Team workspace:\n\n"
        f"{_frontend_link('/invitations/accept', token)}\n\n"
        "If you did not expect this invitation, you can ignore this message."
    )
    await _deliver_message(message)
