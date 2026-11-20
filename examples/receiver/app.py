"""Minimal receiver that durably accepts each signed event once."""

import asyncio
import hmac
import math
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from webhook_platform_sdk import (
    DEFAULT_MAX_BODY_BYTES,
    ReceiverVerificationError,
    verify_request,
)

app = FastAPI()
secret = os.environ["WEBHOOK_SIGNING_SECRET"]
database_path = Path(os.getenv("RECEIVER_DATABASE", "receiver.db"))

# Bytes read off the wire are capped as they stream in, not after
# buffering, regardless of Content-Length/chunked framing.
max_body_bytes = int(
    os.getenv("WEBHOOK_RECEIVER_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
)
if max_body_bytes <= 0:
    raise RuntimeError("WEBHOOK_RECEIVER_MAX_BODY_BYTES must be positive")

# A byte cap alone does not stop a slow/stalled client from holding the
# read open indefinitely while trickling bytes in under the cap; bound the
# whole read by wall-clock time too.
body_timeout_seconds = float(os.getenv("RECEIVER_BODY_TIMEOUT_SECONDS", "30"))
if not math.isfinite(body_timeout_seconds) or body_timeout_seconds <= 0:
    raise RuntimeError(
        "RECEIVER_BODY_TIMEOUT_SECONDS must be a finite, positive number"
    )

# Optional, off by default, additive to signature verification -- the
# platform never sends an Authorization header to receivers.
bearer_token = os.getenv("WEBHOOK_RECEIVER_BEARER_TOKEN")
if bearer_token is not None and not bearer_token:
    raise RuntimeError("WEBHOOK_RECEIVER_BEARER_TOKEN must not be empty")


def _check_bearer(request: Request) -> None:
    if bearer_token is None:
        return
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    valid = scheme.lower() == "bearer" and hmac.compare_digest(
        value.encode("utf-8", errors="replace"),
        bearer_token.encode("utf-8"),
    )
    if not valid:
        raise HTTPException(status_code=401, detail="invalid bearer token")


def accept_once(event_id: str, event_type: str, body: bytes) -> bool:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS inbox (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            body BLOB NOT NULL,
            accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        try:
            connection.execute(
                "INSERT INTO inbox(event_id, event_type, body) "
                "VALUES (?, ?, ?)",
                (event_id, event_type, body),
            )
        except sqlite3.IntegrityError:
            return False
    return True


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Stream into one bytearray, capped at ``limit`` bytes regardless of
    Content-Length/chunked framing or chunk count."""
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="payload too large")
        buffer += chunk
    return bytes(buffer)


@app.post("/webhooks", status_code=202)
async def receive(request: Request) -> Response:
    _check_bearer(request)
    try:
        async with asyncio.timeout(body_timeout_seconds):
            raw_body = await _read_bounded_body(request, max_body_bytes)
    except TimeoutError:
        raise HTTPException(
            status_code=408, detail="request body timed out"
        ) from None
    try:
        event = verify_request(
            raw_body,
            request.headers,
            secret,
            max_body_bytes=max_body_bytes,
        )
    except ReceiverVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not accept_once(event.event_id, event.event_type, raw_body):
        # A verified duplicate is success, not an error that should be retried.
        return Response(status_code=204)
    return Response(status_code=202)
