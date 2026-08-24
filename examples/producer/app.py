"""Send one stable example event."""

import os

from webhook_platform_sdk import Producer

base_url = os.getenv("WEBHOOK_PLATFORM_URL", "http://localhost:8000")
api_key = os.environ["WEBHOOK_PLATFORM_API_KEY"]

with Producer(base_url, api_key) as producer:
    event = producer.send_event(
        "example.order.created",
        {"order_id": "example-42", "total": 42},
        "example-order-42-created",
    )
print(f"accepted event {event['public_id']}")
