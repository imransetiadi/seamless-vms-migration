"""An RFC 2136 DNS server for tests (SDD §8.4): one zone's RRsets, queries and UPDATEs over TCP,
each message's TSIG signature checked with its key - a message signed with another key is refused
(NOTAUTH) - and every UPDATE kept for the test to read. Synthetic names and a synthetic key."""

from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import dataclass, field

import dns.message
import dns.opcode
import dns.rcode
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.tsig
import dns.tsigkeyring

KEY_NAME = "seamless-swap."
#: the synthetic TSIG secret's bytes and their base64, as the secret store holds it (SDD §13.3)
KEY_BYTES = b"synthetic-tsig-key-for-tests-only"
KEY_SECRET = base64.b64encode(KEY_BYTES).decode()


@dataclass
class FakeDnsServer:
    """``rrsets``: (name, type) -> (ttl, {value}); ``updates``: the UPDATE messages it received."""

    rrsets: dict[tuple[str, str], tuple[int, set[str]]] = field(default_factory=dict)
    updates: list[dns.message.Message] = field(default_factory=list)
    queries: int = 0
    #: the rcode every UPDATE is refused with, when set
    refuse: int | None = None
    port: int = 0
    _server: asyncio.base_events.Server | None = None

    def __post_init__(self) -> None:
        self.keyring = dns.tsigkeyring.from_text({KEY_NAME: KEY_SECRET})

    async def __aenter__(self) -> FakeDnsServer:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    size = struct.unpack("!H", await reader.readexactly(2))[0]
                except asyncio.IncompleteReadError:
                    return
                out = self._answer(await reader.readexactly(size)).to_wire()
                writer.write(struct.pack("!H", len(out)) + out)
                await writer.drain()
        finally:
            writer.close()

    def _answer(self, wire: bytes) -> dns.message.Message:
        try:
            msg = dns.message.from_wire(wire, keyring=self.keyring)
        except dns.tsig.PeerError:
            msg = dns.message.from_wire(wire, keyring=None, ignore_trailing=True)
            refused = dns.message.make_response(msg)
            refused.set_rcode(dns.rcode.NOTAUTH)
            return refused
        response = dns.message.make_response(msg)
        if msg.opcode() == dns.opcode.UPDATE:
            self.updates.append(msg)
            if self.refuse is not None:
                response.set_rcode(self.refuse)
                return response
            for rrset in msg.prerequisite:
                key = (rrset.name.to_text(), dns.rdatatype.to_text(rrset.rdtype))
                if {r.to_text() for r in rrset} != self.rrsets.get(key, (0, set()))[1]:
                    response.set_rcode(dns.rcode.NXRRSET)
                    return response
            for rrset in msg.update:
                key = (rrset.name.to_text(), dns.rdatatype.to_text(rrset.rdtype))
                if rrset.rdclass == dns.rdataclass.ANY:
                    self.rrsets.pop(key, None)
                else:
                    self.rrsets[key] = (rrset.ttl, {r.to_text() for r in rrset})
            return response
        self.queries += 1
        question = msg.question[0]
        key = (question.name.to_text(), dns.rdatatype.to_text(question.rdtype))
        if key in self.rrsets:
            ttl, values = self.rrsets[key]
            rrset = response.find_rrset(
                response.answer, question.name, dns.rdataclass.IN, question.rdtype, create=True
            )
            for value in values:
                rrset.add(dns.rdata.from_text(dns.rdataclass.IN, question.rdtype, value), ttl)
        return response
