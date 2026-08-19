"""Black-box proof for the Compose worker egress boundary."""

from __future__ import annotations

import socket
import ssl
import struct
import time

PROXY = ("egress-proxy", 3128)
DNS = ("egress-dns", 53)
SECRET_MARKER = "phase3-secret-must-not-appear"


def read_headers(connection: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 16_384:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def tunnel(host: str, port: int = 443) -> tuple[socket.socket, int]:
    connection = socket.create_connection(PROXY, timeout=8)
    authority = f"{host}:{port}"
    connection.sendall(
        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()
    )
    headers = read_headers(connection)
    status = int(headers.split(b" ", 2)[1])
    return connection, status


def expect_denied(host: str, port: int = 443) -> None:
    connection, status = tunnel(host, port)
    connection.close()
    if status != 403:
        raise AssertionError(f"expected proxy 403, got {status}")


def query_rebind_once() -> None:
    labels = b"".join(
        bytes((len(label),)) + label.encode() for label in "rebind.test".split(".")
    ) + b"\x00"
    packet = struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0)
    packet += labels + struct.pack("!HH", 1, 1)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(3)
    client.sendto(packet, DNS)
    response, _ = client.recvfrom(4096)
    client.close()
    if socket.inet_aton("93.184.216.34") not in response:
        raise AssertionError("controlled DNS did not return the public first view")


def prove_direct_egress_is_blocked() -> None:
    try:
        direct = socket.create_connection(("www.example.com", 443), timeout=3)
    except OSError:
        return
    direct.close()
    raise AssertionError("worker network unexpectedly permits direct egress")

def prove_tls_and_sni() -> None:
    connection, status = tunnel("www.example.com")
    if status != 200:
        connection.close()
        raise AssertionError(f"public TLS tunnel returned {status}")
    context = ssl.create_default_context()
    tls = context.wrap_socket(connection, server_hostname="www.example.com")
    tls.sendall(b"HEAD / HTTP/1.1\r\nHost: www.example.com\r\nConnection: close\r\n\r\n")
    if not tls.recv(64).startswith(b"HTTP/"):
        raise AssertionError("valid TLS/SNI target did not answer")
    tls.close()

    connection, status = tunnel("www.example.com")
    if status != 200:
        connection.close()
        raise AssertionError(f"second public TLS tunnel returned {status}")
    try:
        context.wrap_socket(connection, server_hostname="invalid.example")
    except ssl.SSLCertVerificationError:
        connection.close()
    else:
        raise AssertionError("TLS hostname mismatch was accepted")


def send_secret_bearing_denied_request() -> None:
    connection = socket.create_connection(PROXY, timeout=3)
    request = (
        "GET http://private.test/?token="
        + SECRET_MARKER
        + " HTTP/1.1\r\nHost: private.test\r\n\r\n"
    )
    connection.sendall(request.encode())
    headers = read_headers(connection)
    connection.close()
    if b" 403 " not in headers:
        raise AssertionError("non-CONNECT proxy request was not denied")


def main() -> None:
    for _ in range(20):
        try:
            expect_denied("127.0.0.1")
            break
        except OSError:
            time.sleep(0.5)
    else:
        raise AssertionError("egress proxy did not become reachable")

    prove_direct_egress_is_blocked()
    for host in (
        "2130706433",
        "[::ffff:127.0.0.1]",
        "private.test",
        "cname.test",
        "mixed.test",
        "metadata.test",
    ):
        expect_denied(host)
    expect_denied("www.example.com", 8443)

    # The application-side lookup sees public; Squid's independent lookup sees
    # the rebound private address and rejects it before opening a tunnel.
    query_rebind_once()
    expect_denied("rebind.test")
    expect_denied("split.test")
    prove_tls_and_sni()
    send_secret_bearing_denied_request()
    print("Phase 3 egress boundary checks passed")


if __name__ == "__main__":
    main()
