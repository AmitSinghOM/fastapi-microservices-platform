import asyncio
import logging
import socket

import httpx
import pytest

import app.services.delivery_service as delivery_module
import app.worker as worker_module
from app.config import Settings
from app.security_observability import (
    SecurityDenyReason,
    SecurityLayer,
    record_security_deny,
    security_deny_counts,
)
from app.services.delivery_service import DeliveryService
from app.tests.test_delivery_contracts import worker_settings
from app.tests.test_phase2_safety import fake_claim
from app.webhook_security import UnsafeWebhookUrl, validate_webhook_url


async def install_dns_answers(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(*args, **kwargs):
        del args, kwargs
        results = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            results.append(
                (family, socket.SOCK_STREAM, 6, "", (address, 443))
            )
        return results

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/hook", SecurityDenyReason.SCHEME_NOT_ALLOWED),
        (
            "https://user:secret@example.com/hook",
            SecurityDenyReason.CREDENTIALS_FORBIDDEN,
        ),
        (
            "https://example.com/hook#secret",
            SecurityDenyReason.FRAGMENT_FORBIDDEN,
        ),
        (
            "https://example.com:8443/hook",
            SecurityDenyReason.PORT_NOT_ALLOWED,
        ),
        ("https://localhost/hook", SecurityDenyReason.LOCALHOST_FORBIDDEN),
        ("https://127.0.0.1/hook", SecurityDenyReason.NON_GLOBAL_ADDRESS),
        (
            "https://[::ffff:127.0.0.1]/hook",
            SecurityDenyReason.NON_GLOBAL_ADDRESS,
        ),
        (
            "https://169.254.169.254/latest",
            SecurityDenyReason.NON_GLOBAL_ADDRESS,
        ),
    ],
)
async def test_url_policy_rejects_bypass_corpus(
    url: str, reason: SecurityDenyReason
):
    with pytest.raises(UnsafeWebhookUrl) as raised:
        await validate_webhook_url(url)
    assert raised.value.reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostname", ["2130706433", "cname.example", "split.example"]
)
async def test_resolved_alternate_and_private_views_are_rejected(
    hostname: str, monkeypatch: pytest.MonkeyPatch
):
    await install_dns_answers(monkeypatch, ["127.0.0.1"])
    with pytest.raises(UnsafeWebhookUrl) as raised:
        await validate_webhook_url(f"https://{hostname}/hook")
    assert raised.value.reason == SecurityDenyReason.NON_GLOBAL_ADDRESS


@pytest.mark.asyncio
async def test_every_mixed_dns_answer_must_be_global(
    monkeypatch: pytest.MonkeyPatch,
):
    await install_dns_answers(monkeypatch, ["8.8.8.8", "10.0.0.7"])
    with pytest.raises(UnsafeWebhookUrl) as raised:
        await validate_webhook_url("https://mixed.example/hook")
    assert raised.value.reason == SecurityDenyReason.NON_GLOBAL_ADDRESS


@pytest.mark.asyncio
async def test_global_dns_answers_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
):
    await install_dns_answers(monkeypatch, ["8.8.8.8", "2606:4700:4700::1111"])
    url = "https://receiver.example/hook?tenant=public"
    assert await validate_webhook_url(url) == url


def deployed_settings(**changes) -> Settings:
    values = {
        "_env_file": None,
        "environment": "production",
        "auto_create_schema": False,
        "secret_key": "s" * 32,
        "api_key_pepper": "p" * 32,
        "webhook_signing_key": "w" * 32,
        "allow_http_webhooks": False,
        "worker_egress_proxy_url": "http://egress-proxy:3128",
    }
    values.update(changes)
    return Settings(**values)


def test_deployment_requires_explicit_egress_proxy():
    with pytest.raises(ValueError, match="WORKER_EGRESS_PROXY_URL"):
        deployed_settings(worker_egress_proxy_url=None)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "https://egress-proxy:3128",
        "http://user:secret@egress-proxy:3128",
        "http://egress-proxy:3128/path",
        "http://egress-proxy:3128?token=secret",
        "http://localhost:3128",
        "http://egress-proxy",
    ],
)
def test_proxy_url_must_be_credential_free_internal_http(proxy_url: str):
    with pytest.raises(ValueError, match="credential-free"):
        deployed_settings(worker_egress_proxy_url=proxy_url)


def test_worker_client_pins_proxy_and_disables_bypasses(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict = {}

    class CapturingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(worker_module.httpx, "AsyncClient", CapturingClient)
    settings = worker_settings().model_copy(
        update={"worker_egress_proxy_url": "http://egress-proxy:3128"}
    )
    worker_module.create_http_client(settings)

    assert captured["proxy"] == "http://egress-proxy:3128"
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert captured["limits"].max_keepalive_connections == 0


@pytest.mark.asyncio
async def test_proxy_connect_rejection_is_sanitized_and_counted(
    monkeypatch: pytest.MonkeyPatch,
):
    async def allow_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    def reject(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("403 Forbidden", request=request)

    monkeypatch.setattr(delivery_module, "validate_webhook_url", allow_target)
    transport = httpx.MockTransport(reject)
    async with httpx.AsyncClient(transport=transport) as client:
        service = DeliveryService(
            lambda: None,  # type: ignore[arg-type]
            client,
            worker_settings(),
        )
        result = await service._perform_attempt(fake_claim(1))

    assert result.error == "egress_proxy_denied"
    assert result.retryable is False
    assert security_deny_counts() == {
        ("proxy", "proxy_connect_denied"): 1
    }


@pytest.mark.asyncio
async def test_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch):
    async def allow_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1"})

    monkeypatch.setattr(delivery_module, "validate_webhook_url", allow_target)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), follow_redirects=False
    ) as client:
        service = DeliveryService(
            lambda: None,  # type: ignore[arg-type]
            client,
            worker_settings(),
        )
        result = await service._perform_attempt(fake_claim(2))

    assert len(requests) == 1
    assert result.status_code == 302
    assert result.error == "http_status"


@pytest.mark.asyncio
async def test_deny_audit_contains_only_bounded_fields(
    caplog: pytest.LogCaptureFixture,
):
    secret = "destination-password-must-not-leak"
    with pytest.raises(UnsafeWebhookUrl) as raised:
        await validate_webhook_url(
            f"https://user:{secret}@private.example/hook?token={secret}"
        )

    with caplog.at_level(logging.WARNING, logger="app.security.egress"):
        record_security_deny(SecurityLayer.ADMISSION, raised.value.reason)

    assert secret not in caplog.text
    assert "private.example" not in caplog.text
    assert "layer=admission" in caplog.text
    assert "reason=credentials_forbidden" in caplog.text
    assert security_deny_counts() == {
        ("admission", "credentials_forbidden"): 1
    }
