"""Controlled DNS responder used only by the Phase 3 container harness.

The responder is authoritative for a fixed set of test names and never
forwards: unknown names receive NXDOMAIN, so the harness cannot leak a lookup
to a real resolver. "Public" answers point at the simulated receiver on an
internal Docker network whose address is outside every production deny rule.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from collections import Counter

RECEIVER_NAME = "receiver.public.test"
RECEIVER_ADDRESS = "11.200.0.10"

TYPE_A = 1
TYPE_CNAME = 5
TYPE_AAAA = 28
FLAGS_NOERROR = 0x8580
FLAGS_NXDOMAIN = 0x8583

STATIC_ADDRESSES: dict[str, tuple[str, ...]] = {
    RECEIVER_NAME: (RECEIVER_ADDRESS,),
    "private.test": ("127.0.0.1",),
    "split.test": ("127.0.0.1",),
    "metadata.test": ("169.254.169.254",),
    "mixed.test": (RECEIVER_ADDRESS, "127.0.0.1"),
}
CNAMES = {"cname.test": "private.test"}
REBIND_NAME = "rebind.test"
KNOWN_NAMES = frozenset({*STATIC_ADDRESSES, *CNAMES, REBIND_NAME})


def decode_name(packet: bytes, offset: int = 12) -> tuple[str, int]:
    labels: list[str] = []
    while packet[offset]:
        length = packet[offset]
        offset += 1
        labels.append(packet[offset : offset + length].decode("ascii"))
        offset += length
    return ".".join(labels).lower(), offset + 1


def encode_name(name: str) -> bytes:
    return (
        b"".join(
            bytes((len(label),)) + label.encode("ascii")
            for label in name.rstrip(".").split(".")
        )
        + b"\x00"
    )


def address_answer(owner: bytes, address: str, ttl: int = 0) -> bytes:
    ip = ipaddress.ip_address(address)
    record_type = TYPE_A if ip.version == 4 else TYPE_AAAA
    packed = ip.packed
    return (
        owner + struct.pack("!HHIH", record_type, 1, ttl, len(packed)) + packed
    )


def cname_answer(owner: bytes, target: str) -> bytes:
    encoded = encode_name(target)
    return (
        owner + struct.pack("!HHIH", TYPE_CNAME, 1, 0, len(encoded)) + encoded
    )


class DnsServer:
    def __init__(self) -> None:
        self.rebind_queries: Counter[str] = Counter()

    def answers(self, name: str, query_type: int) -> list[bytes]:
        owner = b"\xc0\x0c"
        if query_type != TYPE_A:
            return []
        if name == REBIND_NAME:
            self.rebind_queries[name] += 1
            first_view = self.rebind_queries[name] == 1
            address = RECEIVER_ADDRESS if first_view else "127.0.0.1"
            return [address_answer(owner, address)]
        if name in CNAMES:
            target = CNAMES[name]
            return [cname_answer(owner, target)] + [
                address_answer(encode_name(target), address)
                for address in STATIC_ADDRESSES[target]
            ]
        return [
            address_answer(owner, address)
            for address in STATIC_ADDRESSES.get(name, ())
        ]

    def response(self, packet: bytes) -> bytes:
        name, question_end = decode_name(packet)
        query_type = struct.unpack(
            "!H", packet[question_end : question_end + 2]
        )[0]
        question_end += 4
        answers = self.answers(name, query_type)
        flags = FLAGS_NOERROR if name in KNOWN_NAMES else FLAGS_NXDOMAIN
        header = struct.pack(
            "!HHHHHH",
            struct.unpack("!H", packet[:2])[0],
            flags,
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
