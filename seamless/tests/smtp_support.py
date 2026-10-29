"""An SMTP relay in the test process (SDD §24.10): aiosmtpd with STARTTLS or implicit TLS on a
certificate the test makes, AUTH after TLS only, and the replies a scenario asks for."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import Message
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def make_certificate(directory: Path, name: str = "127.0.0.1") -> tuple[Path, Path]:
    """A self-signed certificate for the loopback relay, and its key, as PEM files."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(name))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "relay.crt", directory / "relay.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    """The relay's handler: what it received, with the replies the scenario scripted."""

    def __init__(self, *, refuse: tuple[str, ...] = (), data_reply: str | None = None) -> None:
        self.refuse = refuse
        self.data_reply = data_reply
        self.messages: list[Message] = []
        self.sessions_tls: list[bool] = []
        self.recipients: list[list[str]] = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address in self.refuse:
            return "550 5.1.1 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(message_from_bytes(envelope.content))
        # STARTTLS sets session.ssl; implicit TLS shows on the transport alone
        transport = getattr(server, "transport", None)
        over_tls = transport is not None and transport.get_extra_info("ssl_object") is not None
        self.sessions_tls.append(session.ssl is not None or over_tls)
        self.recipients.append(list(envelope.rcpt_tos))
        return self.data_reply or "250 OK"


class Relay:
    """aiosmtpd on a loopback port: ``starttls`` (required), ``implicit`` TLS, or plain."""

    def __init__(
        self,
        certificate: tuple[Path, Path] | None,
        *,
        mode: str = "starttls",
        auth: tuple[str, str] | None = None,
        handler: Recorder | None = None,
    ) -> None:
        self.handler = handler or Recorder()
        self.port = free_port()
        self.auth = auth
        self.logins: list[str] = []
        context = None
        if certificate is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificate[0]), str(certificate[1]))
        kwargs: dict = {}
        if mode == "starttls":
            kwargs["server_kwargs"] = {"tls_context": context, "require_starttls": True}
        elif mode == "implicit":
            kwargs["ssl_context"] = context
        if auth is not None:
            kwargs.update(
                authenticator=self._authenticate, auth_required=True, auth_require_tls=True
            )
        self.controller = Controller(self.handler, hostname="127.0.0.1", port=self.port, **kwargs)

    def _authenticate(self, server, session, envelope, mechanism, auth_data):
        assert self.auth is not None
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        user, password = auth_data.login.decode(), auth_data.password.decode()
        self.logins.append(user)
        if (user, password) == self.auth:
            return AuthResult(success=True)
        return AuthResult(success=False, handled=False)  # the server answers 535 itself

    def __enter__(self) -> Relay:
        self.controller.start()
        return self

    def __exit__(self, *exc) -> None:
        self.controller.stop()
