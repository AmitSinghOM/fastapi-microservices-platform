"""Simulated public HTTPS receiver for the egress harness.

It serves the fixture certificate on TCP 443 and echoes the SNI it observed
in a response header, so the probe can prove the proxy tunnel preserved
SNI end to end. Request contents are never logged.
"""

from __future__ import annotations

import socketserver
import ssl
import sys
import threading
from pathlib import Path

SNI_HEADER = "X-Observed-SNI"

_handshake = threading.local()


def server_context(fixture: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        fixture / "receiver.pem", fixture / "receiver-key.pem"
    )

    def remember_sni(
        _: ssl.SSLObject | ssl.SSLSocket,
        server_name: str | None,
        __: ssl.SSLContext,
    ) -> None:
        _handshake.server_name = server_name

    context.sni_callback = remember_sni
    return context


class ReceiverHandler(socketserver.BaseRequestHandler):
    server: ReceiverServer

    def handle(self) -> None:
        _handshake.server_name = None
        self.request.settimeout(5)
        try:
            tls = self.server.context.wrap_socket(
                self.request, server_side=True
            )
        except (ssl.SSLError, OSError):
            return
        with tls:
            try:
                request = b""
                while b"\r\n\r\n" not in request and len(request) < 16_384:
                    chunk = tls.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                observed = _handshake.server_name or "none"
                tls.sendall(
                    b"HTTP/1.1 204 No Content\r\n"
                    + f"{SNI_HEADER}: {observed}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                )
            except (ssl.SSLError, OSError):
                return


class ReceiverServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], context: ssl.SSLContext
    ) -> None:
        self.context = context
        super().__init__(address, ReceiverHandler)


if __name__ == "__main__":
    fixture_directory = Path(sys.argv[1] if len(sys.argv) > 1 else "/fixture")
    with ReceiverServer(
        ("0.0.0.0", 443), server_context(fixture_directory)
    ) as receiver:
        receiver.serve_forever()
