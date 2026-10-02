"""Exercise the shipped worker HTTP client inside the egress harness.

Started by scripts/verify_egress_boundary.py with HTTPS_PROXY, NO_PROXY and
SSL_CERT_FILE set to hostile values. The client must still tunnel through
the pinned proxy and must not adopt the harness CA from SSL_CERT_FILE, so a
regression to trust_env=True fails here instead of passing silently.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx

from app.config import Settings
from app.worker import create_http_client

PROXY_URL = "http://egress-proxy:3128"
RECEIVER_URL = "https://receiver.public.test/"


def caused_by_certificate_rejection(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


async def main() -> None:
    settings = Settings(environment="test", worker_egress_proxy_url=PROXY_URL)
    async with create_http_client(settings) as client:
        try:
            await client.get("https://private.test/")
        except httpx.ProxyError:
            pass
        else:
            raise AssertionError("worker client reached a private target")
        try:
            await client.get(RECEIVER_URL)
        except httpx.ConnectError as error:
            # The handshake can only start after Squid opened the tunnel.
            if not caused_by_certificate_rejection(error):
                raise AssertionError(
                    "worker client did not reach the receiver via the proxy"
                ) from error
        else:
            raise AssertionError("worker client trusted SSL_CERT_FILE")


if __name__ == "__main__":
    asyncio.run(main())
