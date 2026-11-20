"""SQLite concurrency safety and unsupported-dialect rejection for the outbox.

These exercise the dialect-specific claim strategies in ``outbox.py``
directly. ``test_outbox.py`` only ever runs single-threaded SQLite
(in-memory, no real concurrency); it does not cover PostgreSQL -- that is
exercised separately by the Postgres-marked suite against a live server.
Here, SQLite gets its own per-row compare-and-set path, since
``with_for_update`` is a silent no-op on that dialect.
"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import Session, sessionmaker

from webhook_platform_sdk.outbox import (
    OutboxRelay,
    UnsupportedDialectError,
    create_outbox_schema,
    enqueue_outbox,
    outbox_events,
    utcnow,
)


class RecordingProducer:
    """Thread-safe producer stub that records every idempotency key sent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent_keys: list[str] = []

    def send_event(
        self, event_type, payload, idempotency_key, *, envelope_mode="native"
    ):
        with self._lock:
            self.sent_keys.append(idempotency_key)
        return {"public_id": idempotency_key}


def _file_sessionmaker(tmp_path):
    db_path = tmp_path / "outbox.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    create_outbox_schema(engine)
    sessions = sessionmaker(engine, class_=Session, expire_on_commit=False)
    return engine, sessions


def _seed(sessions, count: int) -> None:
    with sessions() as session, session.begin():
        for i in range(count):
            enqueue_outbox(
                session,
                "order.created",
                {"i": i},
                idempotency_key=f"order-{i}",
            )


def test_sqlite_concurrent_relays_never_double_send(tmp_path) -> None:
    """Two relays racing against one real file-backed SQLite db.

    A ``before_cursor_execute`` hook forces both relays' unlocked candidate
    ``SELECT`` to complete *before either does its per-row claim UPDATE*,
    by blocking each on a 2-party barrier the first time it sees that
    SELECT. This deterministically reproduces the race the compare-and-set
    fix guards against (both threads see the same row as unclaimed) rather
    than relying on scheduling luck. Without the fix, both relays would
    then also both succeed at claiming and sending the same row.
    """
    engine, sessions = _file_sessionmaker(tmp_path)
    _seed(sessions, 20)
    producer = RecordingProducer()
    relay_a = OutboxRelay(sessions, producer, batch_size=20)
    relay_b = OutboxRelay(sessions, producer, batch_size=20)

    sync_barrier = threading.Barrier(2, timeout=5)
    seen_lock = threading.Lock()
    seen = {"count": 0}

    def sync_on_candidate_select(
        conn, cursor, statement, parameters, context, executemany
    ):
        if (
            statement.strip().upper().startswith("SELECT")
            and "webhook_outbox" in statement
        ):
            with seen_lock:
                seen["count"] += 1
                should_wait = seen["count"] <= 2
            if should_wait:
                sync_barrier.wait()

    event.listen(engine, "before_cursor_execute", sync_on_candidate_select)
    try:
        results: dict[str, tuple[int, int]] = {}
        errors: dict[str, BaseException] = {}

        def run(name: str, relay: OutboxRelay) -> None:
            try:
                results[name] = relay.run_once()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors[name] = exc

        t1 = threading.Thread(target=run, args=("a", relay_a))
        t2 = threading.Thread(target=run, args=("b", relay_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive(), "relay a did not finish"
        assert not t2.is_alive(), "relay b did not finish"
    finally:
        event.remove(engine, "before_cursor_execute", sync_on_candidate_select)

    assert errors == {}
    assert set(results) == {"a", "b"}
    assert seen["count"] == 2  # both relays actually raced the same read
    assert len(producer.sent_keys) == 20
    assert len(set(producer.sent_keys)) == 20  # no key sent twice
    assert results["a"][0] + results["b"][0] == 20


def test_sqlite_lease_blocks_reclaim_until_expiry(tmp_path) -> None:
    _engine, sessions = _file_sessionmaker(tmp_path)
    _seed(sessions, 1)
    stalled_producer_calls: list[str] = []

    class StallingProducer:
        def send_event(self, *args, **kwargs):
            stalled_producer_calls.append(args[2])
            raise RuntimeError("send never finishes before lease check")

    relay = OutboxRelay(sessions, StallingProducer(), lease_seconds=60)
    first = relay._claim()
    assert len(first) == 1

    # Still leased: a second claim attempt must not pick up the same row.
    second = relay._claim()
    assert second == []

    # Expire the lease directly, as if the lease window had passed.
    with sessions() as session, session.begin():
        session.execute(
            update(outbox_events).values(
                locked_until=utcnow() - timedelta(seconds=1)
            )
        )

    third = relay._claim()
    assert len(third) == 1
    assert third[0].id == first[0].id
    assert third[0].attempt_number == first[0].attempt_number + 1


def test_postgresql_dialect_uses_for_update_path(monkeypatch) -> None:
    """Dispatch picks the PostgreSQL path without needing a live server."""
    from webhook_platform_sdk import outbox as outbox_module

    calls: list[str] = []

    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_bind(self):
            return FakeBind()

    relay = OutboxRelay(FakeSession, object())
    monkeypatch.setattr(
        outbox_module.OutboxRelay,
        "_claim_for_update",
        lambda self, now: calls.append("for_update") or [],
    )
    monkeypatch.setattr(
        outbox_module.OutboxRelay,
        "_claim_compare_and_set",
        lambda self, now: calls.append("compare_and_set") or [],
    )
    assert relay._claim() == []
    assert calls == ["for_update"]


def test_unsupported_dialect_is_rejected_explicitly() -> None:
    """MySQL (or anything else) is rejected rather than silently handled
    by either the PostgreSQL or SQLite strategy."""

    class FakeDialect:
        name = "mysql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_bind(self):
            return FakeBind()

    relay = OutboxRelay(FakeSession, object())
    with pytest.raises(UnsupportedDialectError, match="mysql"):
        relay._claim()
