"""The email sender (SDD §8.7, §24.3, §24.5, plan task NT6): ``smtplib`` in at most two threads of
the notifier's own; STARTTLS or implicit TLS, never plain text; the relay's certificate verified;
AUTH after TLS alone; a 4yz reply retried, a 5yz reply final."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

from ..config import Settings
from ..domain.models import NotificationChannel
from ..security.secrets import SecretNotFound, read_secret
from .messages import Message, email_message
from .notifier import SendResult

log = logging.getLogger(__name__)

#: an SMTP session has this long (SDD §24.6)
SMTP_TIMEOUT_S = 30.0


def relay_configured(settings: Settings) -> bool:
    """Whether the installation has a relay: SEAMLESS_SMTP_HOST and SEAMLESS_SMTP_FROM (§24.3)."""
    return bool(settings.smtp_host and settings.smtp_from)


def render_email(
    settings: Settings, channel: NotificationChannel, message: Message, key: str
) -> EmailMessage:
    """The email the sender submits (SDD §24.5): From SEAMLESS_SMTP_FROM, To the channel's
    recipients, the key in the Message-ID at the sender's domain - what the demo's recording
    sender renders too (§7.4)."""
    sender = settings.smtp_from or ""
    domain = sender.rpartition("@")[2] or "seamless"
    return email_message(
        message, sender=sender, recipients=channel.recipients, key=key, domain=domain
    )


class EmailSender:
    """One per server: the notifier's email channels send through it."""

    def __init__(self, settings: Settings, *, timeout_s: float = SMTP_TIMEOUT_S) -> None:
        self.settings = settings
        self.timeout_s = timeout_s
        # SDD §24.6: never asyncio's default pool, which the store's calls and the GPU reads need
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="smtp")

    async def send(
        self,
        channel: NotificationChannel,
        message: Message,
        key: str,
        secret: Mapping[str, str] | None,
    ) -> SendResult:
        if not relay_configured(self.settings):
            return SendResult("failed", "no_relay")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool, self._send_blocking, channel, message, key, secret
        )

    def _context(self) -> ssl.SSLContext:
        ca = self.settings.smtp_ca_file
        return ssl.create_default_context(cafile=str(ca) if ca else None)

    def _send_blocking(
        self,
        channel: NotificationChannel,
        message: Message,
        key: str,
        secret: Mapping[str, str] | None,
    ) -> SendResult:
        settings = self.settings
        # SEAMLESS_SMTP_SECRET, read as §13.3 resolves a secret each time a message is sent (§8.7):
        # an email channel has no secret of its own, so ``secret`` is not the relay's
        credentials: Mapping[str, str] | None = None
        if settings.smtp_secret:
            try:
                credentials = read_secret(settings.smtp_secret, settings)
            except (SecretNotFound, OSError):
                credentials = None
            if not credentials or not credentials.get("username"):
                return SendResult("failed", "no_secret")
        mail = render_email(settings, channel, message, key)
        host, port = settings.smtp_host or "", settings.smtp_port
        try:
            if settings.smtp_security == "tls":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    host, port, timeout=self.timeout_s, context=self._context()
                )
            else:
                client = smtplib.SMTP(host, port, timeout=self.timeout_s)
            with client:
                if settings.smtp_security != "tls":
                    client.ehlo()
                    if not client.has_extn("starttls"):
                        # no plain-text mode (NFR-06): nothing but EHLO went in clear text
                        return SendResult("failed", "no_starttls")
                    client.starttls(context=self._context())
                    client.ehlo()
                if credentials:
                    # after TLS alone: the session is encrypted here on both paths
                    client.login(credentials["username"], credentials.get("password", ""))
                refused = client.send_message(mail)
                return SendResult("sent", refused=len(refused))
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            outcome = "retry" if codes and all(400 <= c < 500 for c in codes) else "failed"
            return SendResult(outcome, str(codes[0]) if codes else "refused", refused=len(codes))
        except smtplib.SMTPResponseException as exc:
            outcome = "retry" if 400 <= exc.smtp_code < 500 else "failed"
            return SendResult(outcome, str(exc.smtp_code))
        except ssl.SSLError as exc:
            log.warning("relay %s: TLS failed: %s", host, exc.reason or type(exc).__name__)
            return SendResult("retry", "tls_error")
        except (OSError, smtplib.SMTPException) as exc:
            return SendResult("retry", type(exc).__name__)
