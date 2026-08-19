"""Controlled DNS responder used only by the Phase 3 container harness."""

from __future__ import annotations

import ipaddress
import socket
import struct
from collections import Counter
from typing import cast


def decode_name(packet: bytes, offset: int = 12) -> tuple[str, int]:
    labels: list[str] = []
    while packet[offset]:
        length = packet[offset]
        offset += 1
        labels.append(packet[offset : offset + length].decode("ascii"))
        offset += length
    return ".".join(labels).lower(), offset + 1


def encode_name(name: str) -> bytes:
    return b"".join(
        bytes((len(label),)) + label.encode("ascii")
        for label in name.rstrip(".").split(".")
    ) + b"\x00"


def address_answer(owner: bytes, address: str, ttl: int = 0) -> bytes:
    ip = ipaddress.ip_address(address)
    record_type = 1 if ip.version == 4 else 28
    packed = ip.packed
    return owner + struct.pack("!HHIH", record_type, 1, ttl, len(packed)) + packed


def cname_answer(owner: bytes, target: str) -> bytes:
    encoded = encode_name(target)
    return owner + struct.pack("!HHIH", 5, 1, 0, len(encoded)) + encoded


class DnsServer:
    def __init__(self) -> None:
        self.rebind_queries: Counter[str] = Counter()

    def answers(self, name: str, query_type: int) -> list[bytes]:
        owner = b"\xc0\x0c"
        if query_type not in {1, 28}:
            return []
        if name == "rebind.test" and query_type == 1:
            self.rebind_queries[name] += 1
            address = (
                "93.184.216.34"
                if self.rebind_queries[name] == 1
                else "127.0.0.1"
            )
            return [address_answer(owner, address)]
        if name in {"private.test", "split.test", "metadata.test"}:
            if query_type == 1:
                address = (
                    "169.254.169.254"
                    if name == "metadata.test"
                    else "127.0.0.1"
                )
                return [address_answer(owner, address)]
            return []
        if name == "cname.test" and query_type == 1:
            target = "private.test"
            return [
                cname_answer(owner, target),
                address_answer(encode_name(target), "127.0.0.1"),
            ]
        if name == "mixed.test" and query_type == 1:
            return [
                address_answer(owner, "93.184.216.34"),
                address_answer(owner, "127.0.0.1"),
            ]

        family = socket.AF_INET if query_type == 1 else socket.AF_INET6
        try:
            results = socket.getaddrinfo(
                name, None, family=family, type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            return []
        addresses = sorted(
            {cast(str, result[4][0]) for result in results}
        )
        return [
            address_answer(owner, address, ttl=30)
            for address in addresses
            if ipaddress.ip_address(address).is_global
        ]

    def response(self, packet: bytes) -> bytes:
        name, question_end = decode_name(packet)
        query_type = struct.unpack("!H", packet[question_end : question_end + 2])[0]
        question_end += 4
        answers = self.answers(name, query_type)
        header = struct.pack(
            "!HHHHHH",
            struct.unpack("!H", packet[:2])[0],
            0x8180,
            1,
            len(answers),
            0,
            0,
        )
        return header + packet[12:question_end] + b"".join(answers)

    def serve(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("0.0.0.0", 53))
        while True:
            packet, peer = server.recvfrom(4096)
            try:
                server.sendto(self.response(packet), peer)
            except (IndexError, UnicodeError, ValueError, struct.error):
                continue


if __name__ == "__main__":
    DnsServer().serve()
