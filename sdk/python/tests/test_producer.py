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
    producer = Producer(
        "https://ignored.example", "producer-key", client=client
    )
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
    producer = Producer(
        "https://ignored.example", "producer-key", client=client
    )

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


def test_management_client_requires_exactly_one_auth_mode() -> None:
    with pytest.raises(ValueError):
        ManagementClient("https://api.example")
    with pytest.raises(ValueError):
        ManagementClient("https://api.example", "jwt-token", api_key="whk_x")
    with pytest.raises(ValueError):
        ManagementClient("https://api.example", "", api_key="whk_x")
    with pytest.raises(ValueError):
        ManagementClient("https://api.example", "")
    with pytest.raises(ValueError):
        ManagementClient("https://api.example", api_key="")


def test_management_client_machine_auth_sends_api_key_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"eligible_for_replay": False})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    with ManagementClient(
        "https://ignored.example", api_key="whk_abc_producer", client=client
    ) as management:
        result = management.get_delivery_recovery("project-1", "delivery-1")

    assert result == {"eligible_for_replay": False}
    assert requests[0].headers["X-API-Key"] == "whk_abc_producer"
    assert "Authorization" not in requests[0].headers
    assert requests[0].url.path == (
        "/v1/projects/project-1/deliveries/delivery-1/recovery"
    )


def test_create_api_key_defaults_scopes_to_events_write() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"public_id": "key-1"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    with ManagementClient(
        "https://ignored.example", "jwt-token", client=client
    ) as management:
        management.create_api_key("project-1", "producer")

    assert json.loads(requests[0].content)["scopes"] == ["events:write"]


def test_create_api_key_accepts_explicit_scopes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"public_id": "key-1"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    with ManagementClient(
        "https://ignored.example", "jwt-token", client=client
    ) as management:
        management.create_api_key(
            "project-1",
            "machine",
            scopes=["endpoints:read", "deliveries:replay"],
        )

    assert json.loads(requests[0].content)["scopes"] == [
        "endpoints:read",
        "deliveries:replay",
    ]
