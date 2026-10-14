import json
import logging
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage

from .config import get_settings

logger = logging.getLogger(__name__)


def send_password_reset_email(recipient: str, reset_url: str) -> bool:
    """Send password reset instructions via Resend API or SMTP.

    Always prints the message to stdout so local dev and serverless logs retain the link.
    Returns True if sent or simulated locally, False if external delivery failed.
    """
    message = EmailMessage()
    message["Subject"] = "Reset your Ekyama password"
    message["To"] = recipient
    body_text = (
        "We received a request to reset your password.\n\n"
        f"Use this link to reset your password:\n\n{reset_url}\n\n"
        "This link will expire in 30 minutes.\n"
        "If you did not request this, you can safely ignore this email."
    )
    html_content = (
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;line-height:1.6;color:#333;max-width:600px;margin:0 auto;padding:20px;'>"
        "<h2 style='color:#0f172a;'>Reset your Ekyama password</h2>"
        "<p>We received a request to reset your password for your Ekyama validator account.</p>"
        "<p style='margin:28px 0;'>"
        f"<a href='{reset_url}' style='background-color:#0d9488;color:#ffffff;padding:12px 24px;text-decoration:none;border-radius:6px;font-weight:600;display:inline-block;'>Reset Password</a>"
        "</p>"
        f"<p style='font-size:13px;color:#64748b;'>Or copy and paste this link into your browser:<br/><a href='{reset_url}' style='color:#0d9488;word-break:break-all;'>{reset_url}</a></p>"
        "<hr style='border:none;border-top:1px solid #e2e8f0;margin:30px 0;'/>"
        "<p style='font-size:12px;color:#94a3b8;'>This link expires in 30 minutes. If you did not request a password reset, you can safely ignore this email.</p>"
        "</div>"
    )
    message.set_content(body_text)
    message.add_alternative(html_content, subtype="html")

    # Always log the debug email with the reset link for development and serverless debugging
    print(
        f"[email debug]\nTo: {recipient}\nSubject: {message['Subject']}\n\n{body_text}",
        flush=True,
    )

    settings = get_settings()

    # 1. Resend API delivery (recommended for Vercel/serverless environments)
    resend_key = (settings.resend_api_key or "").strip()
    if resend_key:
        sender = (settings.resend_from_email or "").strip() or "Ekyama <onboarding@resend.dev>"
        payload = json.dumps({
            "from": sender,
            "to": [recipient],
            "subject": message["Subject"],
            "text": body_text,
            "html": html_content,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
                "User-Agent": "Ekyama-Mailer/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[email] Successfully delivered reset email to {recipient} via Resend (HTTP {resp.status})", flush=True)
                return True
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            print(f"[email error] Resend HTTP {exc.code} for {recipient}: {err_body}", file=sys.stderr, flush=True)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[email error] Resend delivery failed for {recipient}: {exc}", file=sys.stderr, flush=True)
            return False

    # 2. SMTP delivery
    smtp_host = (settings.smtp_host or "").strip()
    if not smtp_host:
        print(
            f"[email notice] Neither RESEND_API_KEY nor SMTP_HOST is configured in environment variables. "
            f"Reset link generated and logged above, but no external email was dispatched.",
            flush=True,
        )
        return True

    username = (settings.smtp_username or "").strip() or None
    password = (settings.smtp_password or "").strip() or None
    sender = (settings.smtp_from_email or "").strip() or username or "no-reply@ekyama.com"
    message["From"] = sender

    port = int(settings.smtp_port or 587)
    context = ssl.create_default_context()

    try:
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, timeout=10, context=context) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=10) as server:
                server.starttls(context=context)
                if username and password:
                    server.login(username, password)
                server.send_message(message)
        print(f"[email] Successfully delivered reset email to {recipient} via SMTP", flush=True)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        print(
            f"[email error] SMTP authentication failed for {recipient} ({smtp_host}:{port}): {exc}. "
            "For Gmail, you MUST use a 16-character Google App Password (with 2FA enabled), not your normal password.",
            file=sys.stderr,
            flush=True,
        )
        return False
    except (OSError, RuntimeError, smtplib.SMTPException) as exc:
        print(f"[email error] SMTP delivery failed for {recipient} ({smtp_host}:{port}): {exc}", file=sys.stderr, flush=True)
        return False
