from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from backend.core import email


def test_smtp_starttls_uses_certificate_verification_context() -> None:
    message = EmailMessage()
    message["From"] = "no-reply@example.com"
    message["To"] = "user@example.com"
    message.set_content("test")
    smtp_client = MagicMock()
    context = MagicMock()

    with (
        patch("backend.core.email.smtplib.SMTP") as smtp_constructor,
        patch("backend.core.email.ssl.create_default_context", return_value=context),
    ):
        smtp_constructor.return_value.__enter__.return_value = smtp_client
        email._send_smtp(message)

    smtp_client.starttls.assert_called_once_with(context=context)
