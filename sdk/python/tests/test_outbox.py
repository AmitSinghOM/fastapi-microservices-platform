from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from webhook_platform_sdk.outbox import (
    OutboxRelay,
    create_outbox_schema,
    enqueue_outbox,
    outbox_events,
)
from webhook_platform_sdk.producer import ApiError, TransportError


class FlakyProducer:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def send_event(
        self, event_type, payload, idempotency_key, *, envelope_mode="native"
    ):
        self.keys.append(idempotency_key)
        if len(self.keys) == 1:
            raise TransportError()
        return {"public_id": "event-1"}


def test_outbox_uses_caller_transaction_and_stable_retry_key() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_outbox_schema(engine)
    sessions = sessionmaker(engine, class_=Session, expire_on_commit=False)

    with sessions() as session:
        with session.begin():
            enqueue_outbox(
                session,
                "order.created",
                {"order_id": "42"},
                idempotency_key="order-42-created",
            )

    producer = FlakyProducer()
    relay = OutboxRelay(sessions, producer, retry_base_seconds=0)
    assert relay.run_once() == (0, 1)
    assert relay.run_once() == (1, 0)
    assert producer.keys == ["order-42-created", "order-42-created"]

    with sessions() as session:
        row = session.execute(select(outbox_events)).mappings().one()
        assert row["sent_at"] is not None
        assert row["attempt_count"] == 2
        count = session.scalar(
            select(func.count()).select_from(outbox_events)
        )
        assert count == 1


def test_outbox_does_not_retry_terminal_api_error() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_outbox_schema(engine)
    sessions = sessionmaker(engine, class_=Session, expire_on_commit=False)
    with sessions() as session, session.begin():
        enqueue_outbox(
            session,
            "invalid.event",
            {},
            idempotency_key="invalid-1",
        )

    class InvalidProducer:
        def send_event(self, *args, **kwargs):
            raise ApiError(400, "VALIDATION_ERROR")

    relay = OutboxRelay(sessions, InvalidProducer())
    assert relay.run_once() == (0, 1)
    assert relay.run_once() == (0, 0)
    with sessions() as session:
        row = session.execute(select(outbox_events)).mappings().one()
        assert row["failed_at"] is not None
        assert row["last_error_code"] == "VALIDATION_ERROR"
