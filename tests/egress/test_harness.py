"""Hermetic checks for the Phase 3 egress harness itself.

These run without Docker or network access. They pin the properties the
container proof depends on: the controlled resolver never forwards, the
simulated receiver is public yet outside the production deny list, every
harness network is internal, and the throwaway TLS fixture verifies.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import shutil
import socket
import ssl
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "egress"
sys.path.insert(0, str(ROOT))


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dns = load(ROOT / "scripts" / "phase3_dns_server.py")
probe = load(ROOT / "scripts" / "verify_egress_boundary.py")
fixture = load(HARNESS / "tls_fixture.py")
receiver = load(HARNESS / "tls_receiver.py")
smoke = load(HARNESS / "worker_client_smoke.py")


def production_denied_networks() -> list[ipaddress._BaseNetwork]:
    lines = (ROOT / "deploy" / "egress-proxy" / "squid.conf").read_text()
    return [
        ipaddress.ip_network(line.split()[-1])
        for line in lines.splitlines()
        if line.startswith("acl denied_destination dst ")
    ]


def resolve(server: object, name: str) -> tuple[int, list[str]]:
    response = server.response(probe.build_a_query(name))  # type: ignore[attr-defined]
    return response[3] & 0x0F, probe.parse_a_answers(response)


def test_resolver_never_forwards_unknown_names(monkeypatch):
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("harness resolver attempted a real lookup")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    rcode, answers = resolve(dns.DnsServer(), "www.example.com")
    assert (rcode, answers) == (3, [])


def test_resolver_scenarios():
    server = dns.DnsServer()
    assert resolve(server, dns.RECEIVER_NAME) == (0, [dns.RECEIVER_ADDRESS])
    assert resolve(server, "rebind.test")[1] == [dns.RECEIVER_ADDRESS]
    assert resolve(server, "rebind.test")[1] == ["127.0.0.1"]
    assert resolve(server, "mixed.test")[1] == [
        dns.RECEIVER_ADDRESS,
        "127.0.0.1",
    ]
    assert resolve(server, "cname.test")[1] == ["127.0.0.1"]
    assert resolve(server, "metadata.test")[1] == ["169.254.169.254"]


def test_simulated_receiver_is_public_and_allowed_by_production_acls():
    address = ipaddress.ip_address(dns.RECEIVER_ADDRESS)
    denied = production_denied_networks()
    assert len(denied) > 20
    assert address.is_global
    assert not any(address in network for network in denied)
    for hostile in ("127.0.0.1", "169.254.169.254"):
        hostile_address = ipaddress.ip_address(hostile)
        assert any(hostile_address in network for network in denied)


def test_harness_does_not_change_production_policy():
    test_policy = (HARNESS / "squid.test.conf").read_text()
    directives = {
        line.split()[0]
        for line in test_policy.splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert directives <= {
        "dns_nameservers",
        "positive_dns_ttl",
        "negative_dns_ttl",
    }


def test_every_harness_network_is_internal():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    override = yaml.safe_load(
        (HARNESS / "docker-compose.test.yml").read_text()
    )
    networks = {
        name: {**(base["networks"].get(name) or {}), **(settings or {})}
        for name, settings in {
            **base["networks"],
            **override["networks"],
        }.items()
    }
    assert all(
        network.get("internal") is True for network in networks.values()
    )

    subnet = ipaddress.ip_network(
        networks["simulated-public"]["ipam"]["config"][0]["subnet"]
    )
    services = override["services"]
    receiver_address = services["egress-receiver"]["networks"][
        "simulated-public"
    ]["ipv4_address"]
    assert receiver_address == dns.RECEIVER_ADDRESS
    assert ipaddress.ip_address(receiver_address) in subnet
    assert set(services["egress-probe"]["networks"]) == set(
        base["services"]["worker"]["networks"]
    )


def test_harness_names_agree():
    assert probe.RECEIVER_NAME == dns.RECEIVER_NAME == fixture.RECEIVER_NAME


def test_certificate_rejection_is_found_in_cause_chain():
    try:
        try:
            raise ssl.SSLCertVerificationError("hostname mismatch")
        except ssl.SSLError as error:
            raise RuntimeError("connect failed") from error
    except RuntimeError as wrapped:
        assert smoke.caused_by_certificate_rejection(wrapped)
    assert not smoke.caused_by_certificate_rejection(OSError("refused"))


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_tls_fixture_verifies_and_reports_sni(tmp_path):
    fixture.create_fixture(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "ca.pem",
        "receiver-key.pem",
        "receiver.pem",
    ]
    server = receiver.ReceiverServer(
        ("127.0.0.1", 0), receiver.server_context(tmp_path)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = server.server_address[:2]
    trusted = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    try:
        with socket.create_connection(address, timeout=5) as raw:
            with trusted.wrap_socket(
                raw, server_hostname=fixture.RECEIVER_NAME
            ) as tls:
                tls.sendall(b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n")
                headers = probe.read_headers(tls).lower()
        assert headers.startswith(b"http/1.1 204")
        assert probe.SNI_HEADER + b": receiver.public.test" in headers

        for context, name in (
            (trusted, "impostor.public.test"),
            (ssl.create_default_context(), fixture.RECEIVER_NAME),
        ):
            with socket.create_connection(address, timeout=5) as raw:
                with pytest.raises(ssl.SSLCertVerificationError):
                    context.wrap_socket(raw, server_hostname=name)
    finally:
        server.shutdown()
        server.server_close()
