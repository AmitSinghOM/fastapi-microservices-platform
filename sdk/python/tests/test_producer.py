import json

import httpx
import pytest

from webhook_platform_sdk.producer import (
    ApiError,
    AuthClient,
    ManagementClient,
    Producer,
)


def test_producer_sends_one_request_with_stable_idempotency() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={"public_id": "event-1", "envelope_mode": "cloudevents"},
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    producer = Producer("https://ignored.example", "producer-key", client=client)
    result = producer.send_event(
        "order.created",
        {"order_id": "42"},
        "order-42-created",
        envelope_mode="cloudevents",
    )

    assert result["public_id"] == "event-1"
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "order-42-created"
    assert requests[0].headers["X-API-Key"] == "producer-key"
    assert json.loads(requests[0].content)["envelope_mode"] == "cloudevents"


def test_api_error_does_not_expose_response_body() -> None:
    secret_body = "do-not-copy-this-response"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text=secret_body)
        ),
        base_url="https://api.example",
    )
    producer = Producer("https://ignored.example", "producer-key", client=client)

    with pytest.raises(ApiError) as captured:
        producer.send_event("order.created", {}, "order-42")

    assert captured.value.status_code == 500
    assert secret_body not in str(captured.value)


def test_auth_and_management_onboarding_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/users/":
            return httpx.Response(201, json={"id": 1})
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "jwt-token"})
        if request.url.path == "/v1/organizations":
            return httpx.Response(201, json={"public_id": "org-1"})
        if request.url.path.endswith("/api-keys"):
            return httpx.Response(
                201,
                json={"public_id": "key-1", "plaintext_key": "producer-key"},
            )
        raise AssertionError(f"unexpected route {request.url.path}")

    transport = httpx.MockTransport(handler)
    auth_http = httpx.Client(
        transport=transport, base_url="https://api.example"
    )
    with AuthClient("https://ignored.example", client=auth_http) as auth:
        registered = auth.register(
            "owner@example.com", "Owner", "password-value"
        )
        assert registered["id"] == 1
        assert auth.login("owner@example.com", "password-value") == "jwt-token"

    management_http = httpx.Client(
        transport=transport, base_url="https://api.example"
    )
    with ManagementClient(
        "https://ignored.example", "jwt-token", client=management_http
    ) as management:
        organization = management.create_organization("Example")
        assert organization["public_id"] == "org-1"
        key = management.create_api_key("project-1", "producer")
        assert key["public_id"] == "key-1"

    registration = json.loads(requests[0].content)
    assert registration["password"] == "password-value"
    assert requests[1].content == (
        b"username=owner%40example.com&password=password-value"
    )
    assert requests[2].headers["Authorization"] == "Bearer jwt-token"
