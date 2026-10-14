import smtplib
import ssl
from email.message import EmailMessage

from .config import get_settings


def send_password_reset_email(recipient: str, reset_url: str) -> None:
    """Send a password reset link via configured SMTP."""
    message = EmailMessage()
    message["Subject"] = "Reset your Ekyama password"
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your password. "
        f"Use this link to reset your password:\n\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )

    settings = get_settings()
    if settings.smtp_host is None:
        return
    if bool(settings.smtp_username) != bool(settings.smtp_password):
        raise RuntimeError("Configure both SMTP_USERNAME and SMTP_PASSWORD, or neither.")

    sender = settings.smtp_from_email or settings.smtp_username
    if not sender:
        raise RuntimeError("SMTP_FROM_EMAIL is required when SMTP_USERNAME is not configured.")
    message["From"] = sender

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
        server.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)
