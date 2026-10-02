"""Black-box proof for the Compose worker egress boundary.

Every target is local to the harness: the controlled DNS responder answers
all names and the "public" receiver lives on an internal Docker network, so
the probe never contacts a real host or resolver.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import struct
import subprocess
import sys

PROXY = ("egress-proxy", 3128)
DNS = ("egress-dns", 53)
RECEIVER_NAME = "receiver.public.test"
FIXTURE_CA = os.environ.get("PHASE3_FIXTURE_CA", "/fixture/ca.pem")
WORKER_CLIENT_SMOKE = os.environ.get(
    "PHASE3_WORKER_CLIENT_SMOKE", "/harness/worker_client_smoke.py"
)
SNI_HEADER = b"x-observed-sni"
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
        raise AssertionError(f"expected proxy 403 for {host}:{port}")


def expect_unresolvable(host: str) -> None:
    # Squid's dst ACL cannot match a name without answers, so the refusal
    # may come from policy (403) or from the failed lookup (503). Either way
    # no tunnel opens and no fallback resolver is consulted.
    connection, status = tunnel(host)
    connection.close()
    if status not in {403, 503}:
        raise AssertionError(f"unresolvable {host} returned {status}")


def skip_name(packet: bytes, offset: int) -> int:
    while packet[offset]:
        if packet[offset] & 0xC0 == 0xC0:
            return offset + 2
        offset += packet[offset] + 1
    return offset + 1


def build_a_query(name: str) -> bytes:
    question = b"".join(
        bytes((len(label),)) + label.encode() for label in name.split(".")
    )
    header = struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0)
    return header + question + b"\x00" + struct.pack("!HH", 1, 1)


def parse_a_answers(response: bytes) -> list[str]:
    answer_count = struct.unpack("!H", response[6:8])[0]
    offset = skip_name(response, 12) + 4
    addresses: list[str] = []
    for _ in range(answer_count):
        offset = skip_name(response, offset)
        record_type, _, _, length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        if record_type == 1:
            addresses.append(socket.inet_ntoa(response[offset : offset + 4]))
        offset += length
    return addresses


def query_a(name: str) -> list[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(3)
        client.sendto(build_a_query(name), DNS)
        response, _ = client.recvfrom(4096)
    return parse_a_answers(response)


def public_receiver_address() -> str:
    addresses = query_a(RECEIVER_NAME)
    if len(addresses) != 1 or not ipaddress.ip_address(addresses[0]).is_global:
        raise AssertionError("controlled DNS did not return a public receiver")
    return addresses[0]


def prove_direct_egress_is_blocked(address: str) -> None:
    try:
        direct = socket.create_connection((address, 443), timeout=3)
    except OSError:
        return
    direct.close()
    raise AssertionError("worker network unexpectedly permits direct egress")


def query_rebind_once(public_address: str) -> None:
    if query_a("rebind.test") != [public_address]:
        raise AssertionError(
            "controlled DNS did not return the public first view"
        )


def prove_tls_and_sni() -> None:
    context = ssl.create_default_context(cafile=FIXTURE_CA)
    connection, status = tunnel(RECEIVER_NAME)
    if status != 200:
        connection.close()
        raise AssertionError(f"public TLS tunnel returned {status}")
    with context.wrap_socket(connection, server_hostname=RECEIVER_NAME) as tls:
        tls.sendall(
            f"HEAD / HTTP/1.1\r\nHost: {RECEIVER_NAME}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        headers = read_headers(tls).lower()
    if not headers.startswith(b"http/1.1 204"):
        raise AssertionError("valid TLS/SNI target did not answer")
    expected = SNI_HEADER + b": " + RECEIVER_NAME.encode()
    if expected not in headers:
        raise AssertionError("receiver did not observe the requested SNI")

    connection, status = tunnel(RECEIVER_NAME)
    if status != 200:
        connection.close()
        raise AssertionError(f"second public TLS tunnel returned {status}")
    try:
        context.wrap_socket(connection, server_hostname="impostor.public.test")
    except ssl.SSLCertVerificationError:
        connection.close()
    else:
        raise AssertionError("TLS hostname mismatch was accepted")


def prove_worker_client_ignores_ambient_environment() -> None:
    # Separate process: it imports the shipped worker, which this probe
    # deliberately does not, and inherits the hostile ambient environment.
    subprocess.run([sys.executable, WORKER_CLIENT_SMOKE], check=True)


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
    # Compose starts the probe only after every dependency is healthy.
    expect_denied("127.0.0.1")
    public_address = public_receiver_address()
    prove_direct_egress_is_blocked(public_address)
    for host in (
        "2130706433",
        "[::ffff:127.0.0.1]",
        "private.test",
        "cname.test",
        "mixed.test",
        "metadata.test",
    ):
        expect_denied(host)
    expect_unresolvable("unknown.public.test")
    expect_denied(RECEIVER_NAME, 8443)

    # The application-side lookup sees public; Squid's independent lookup sees
    # the rebound private address and rejects it before opening a tunnel.
    query_rebind_once(public_address)
    expect_denied("rebind.test")
    expect_denied("split.test")
    prove_tls_and_sni()
    prove_worker_client_ignores_ambient_environment()
    send_secret_bearing_denied_request()
    print("Phase 3 egress boundary checks passed")


if __name__ == "__main__":
    main()
