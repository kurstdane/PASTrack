"""
Custom email backend for Gmail that handles SSL certificate issues on Windows.
"""
from __future__ import annotations

import ssl
import smtplib
import socket
from typing import Any
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.core.mail.backends.base import BaseEmailBackend
from django.conf import settings

import json
from urllib.request import Request, urlopen


class GmailEmailBackend(SMTPBackend):
    """
    Custom SMTP backend that bypasses SSL certificate verification issues.
    Useful for development on Windows with Python 3.13+.
    """
    # Type hints to satisfy linters for parent class attributes
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    use_ssl: bool
    timeout: int | None
    connection: Any
    fail_silently: bool

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def open(self):
        """
        Open a connection to the email server.
        Override to handle SSL certificate verification issues.
        """
        if self.connection is not None:
            return False

        import sys
        host = str(self.host or "").strip()
        port = int(self.port or 0)

        force_ipv4 = bool(getattr(settings, "LEGALTRACK_EMAIL_FORCE_IPV4", False))

        def _resolve_ipv4(hostname: str, p: int) -> str | None:
            if not hostname:
                return None
            if all(c.isdigit() or c == "." for c in hostname):
                return None
            try:
                infos = socket.getaddrinfo(hostname, p, family=socket.AF_INET, type=socket.SOCK_STREAM)
                if infos:
                    return infos[0][4][0]
            except Exception:
                return None
            return None

        print(f"[SMTP-DEBUG] Attempting connection to {host}:{port}", file=sys.stderr)
        print(f"[SMTP-DEBUG] Current Settings: TLS={self.use_tls}, SSL={self.use_ssl}, ForceIPv4={force_ipv4}", file=sys.stderr)

        try:
            insecure_tls = bool(getattr(settings, "LEGALTRACK_INSECURE_SMTP_TLS", False)) or bool(getattr(settings, "DEBUG", False))
            context = ssl.create_default_context()
            if insecure_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            # Force SSL for port 465 (SMTP_SSL), STARTTLS for port 587 (SMTP)
            if self.port == 465:
                print("[SMTP-DEBUG] Mode: SMTP_SSL", file=sys.stderr)
                self.connection = smtplib.SMTP_SSL(
                    host, port, timeout=self.timeout, context=context
                )
                active_port = port
            else:
                cand_ports: list[int] = [port] if port else []
                host_l = str(self.host or "").lower()
                if "brevo" in host_l or "sendinblue" in host_l:
                    for p in (2525, 443, 587):
                        if p not in cand_ports:
                            cand_ports.append(p)

                last_exc: Exception | None = None
                active_port = port
                for p in (cand_ports or [port]):
                    try:
                        print(f"[SMTP-DEBUG] Mode: Standard SMTP (port {p})", file=sys.stderr)
                        connect_host = host
                        if force_ipv4:
                            resolved = _resolve_ipv4(host, p)
                            if resolved:
                                connect_host = resolved

                        per_attempt_timeout = self.timeout
                        try:
                            per_attempt_timeout = int(per_attempt_timeout) if per_attempt_timeout is not None else None
                        except Exception:
                            per_attempt_timeout = None
                        if per_attempt_timeout is None:
                            per_attempt_timeout = 8
                        per_attempt_timeout = max(3, min(per_attempt_timeout, 10))

                        conn = smtplib.SMTP(timeout=per_attempt_timeout)
                        conn.connect(connect_host, p)
                        if connect_host != host:
                            try:
                                conn._host = host
                            except Exception:
                                pass
                        self.connection = conn
                        active_port = p
                        break
                    except Exception as e:
                        last_exc = e
                        self.connection = None
                        continue

                if self.connection is None:
                    raise last_exc or TimeoutError("SMTP connection failed")

            # For STARTTLS connections (usually port 587/2525/443)
            if self.use_tls and active_port != 465:
                try:
                    if hasattr(self.connection, "_host"):
                        self.connection._host = host
                    if hasattr(self.connection, "host"):
                        self.connection.host = host
                except Exception:
                    pass
                print("[SMTP-DEBUG] Enabling STARTTLS", file=sys.stderr)
                self.connection.starttls(context=context)

            if self.username:
                print(f"[SMTP-DEBUG] Login: {self.username} (Pass Len: {len(self.password)})", file=sys.stderr)
                self.connection.login(self.username, self.password)
            
            print("[SMTP-DEBUG] Connection and login successful", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[SMTP-DEBUG] Connection/Login FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            if self.fail_silently:
                return False
            raise e


class BrevoApiEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        api_key = (
            getattr(settings, "BREVO_API_KEY", "")
            or getattr(settings, "SENDINBLUE_API_KEY", "")
            or ""
        ).strip()
        if not api_key:
            if self.fail_silently:
                return 0
            raise RuntimeError("BREVO_API_KEY is not configured.")

        sent = 0
        for msg in email_messages:
            if not getattr(msg, "to", None):
                continue

            from_email = (getattr(msg, "from_email", "") or getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
            if not from_email:
                from_email = "no-reply@example.com"

            payload: dict[str, object] = {
                "sender": {"email": from_email},
                "to": [{"email": e} for e in (msg.to or []) if e],
                "subject": getattr(msg, "subject", "") or "",
            }

            text_body = getattr(msg, "body", "") or ""
            html_body = None
            alternatives = getattr(msg, "alternatives", None)
            if alternatives:
                for body, mimetype in alternatives:
                    if mimetype == "text/html":
                        html_body = body
                        break

            if html_body is not None:
                payload["htmlContent"] = str(html_body)
                if text_body:
                    payload["textContent"] = str(text_body)
            else:
                payload["textContent"] = str(text_body)

            body = json.dumps(payload).encode("utf-8")
            req = Request(
                "https://api.brevo.com/v3/smtp/email",
                data=body,
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                    "accept": "application/json",
                },
                method="POST",
            )

            try:
                with urlopen(req, timeout=int(getattr(settings, "EMAIL_TIMEOUT", 10) or 10)) as resp:
                    status = getattr(resp, "status", 200)
                    if status >= 400:
                        raw = resp.read().decode("utf-8", errors="replace")
                        raise RuntimeError(f"Brevo API error {status}: {raw}")
            except Exception:
                if self.fail_silently:
                    continue
                raise

            sent += 1

        return sent
