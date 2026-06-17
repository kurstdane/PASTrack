from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.mail import send_mail


@dataclass(frozen=True)
class NotificationResult:
    emailed: bool = False
    sns_queued: bool = False


def _sns_enabled() -> bool:
    return bool(getattr(settings, "LEGALTRACK_SNS_ENABLED", False))


def send_case_email(*, to_email: str, subject: str, message: str, html_message: str | None = None) -> bool:
    with open("email_log.txt", "a") as f:
        f.write(f"Attempting to send email to: {to_email}\n")
        f.write(f"LEGALTRACK_SEND_CASE_EMAILS: {getattr(settings, 'LEGALTRACK_SEND_CASE_EMAILS', False)}\n")
        f.write(f"LEGALTRACK_SEND_EMAILS: {getattr(settings, 'LEGALTRACK_SEND_EMAILS', True)}\n")
        
    if not to_email:
        return False
    if not bool(getattr(settings, "LEGALTRACK_SEND_CASE_EMAILS", False)):
        return False
    if not bool(getattr(settings, "LEGALTRACK_SEND_EMAILS", True)):
        return False
    try:
        send_mail(
            subject,
            message,
            getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@cebu.gov.ph"),
            [to_email],
            fail_silently=False,
            html_message=html_message,
        )
        with open("email_log.txt", "a") as f:
            f.write("Email sent successfully!\n")
    except Exception as e:
        import sys
        print(f"[SMTP-CASE-EMAIL] FAILED: {e}", file=sys.stderr)
        with open("email_log.txt", "a") as f:
            f.write(f"SMTP FAILED: {type(e).__name__}: {e}\n")
        return False
    return True


def sns_hook(*, event: str, payload: dict[str, Any]) -> bool:
    """SMS hook via Textbelt (when enabled).

    When `LEGALTRACK_SNS_ENABLED=False` this is a no-op.
    """
    if not _sns_enabled():
        return False

    api_key = (getattr(settings, "TEXTBELT_API_KEY", "") or "").strip()
    if not api_key:
        return False

    phone = (payload.get("phone") or "").strip()
    tracking_id = (payload.get("tracking_id") or "").strip()
    deadline = (payload.get("deadline") or "").strip()

    if not phone:
        return False

    # Minimal event -> message mapping.
    if event == "case_returned_to_client":
        msg = f"PAStrack: {tracking_id} returned for correction. Deadline: {deadline}"
    elif event == "case_received":
        msg = f"PAStrack: {tracking_id} marked as received."
    elif event == "case_approved":
        msg = f"PAStrack: {tracking_id} approved."
    elif event == "case_released":
        msg = f"PAStrack: {tracking_id} released."
    else:
        msg = f"PAStrack update: {tracking_id}"

    # Textbelt expects form-encoded POST.
    import json
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    data = {
        "phone": phone,
        "message": msg,
        "key": api_key,
    }
    region = (getattr(settings, "TEXTBELT_REGION", "") or "").strip()
    if region:
        data["region"] = region

    body = urlencode(data).encode("utf-8")
    req = Request("https://textbelt.com/text", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})

    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
    except Exception:
        return False

    return bool(isinstance(parsed, dict) and parsed.get("success") is True)

