import json

import httpx
import pytest

from webhook_platform_sdk.producer import ApiError, Producer


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
