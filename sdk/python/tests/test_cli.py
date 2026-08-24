import json

from webhook_platform_sdk import cli


class FakeProducer:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def send_event(
        self, event_type, payload, idempotency_key, *, envelope_mode="native"
    ):
        assert event_type == "order.created"
        assert payload == {"order_id": "42"}
        assert idempotency_key == "order-42-created"
        assert envelope_mode == "cloudevents"
        return {"public_id": "event-1"}


def test_event_command_reads_payload_file(monkeypatch, tmp_path, capsys) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text('{"order_id":"42"}', encoding="utf-8")
    monkeypatch.setattr(cli, "_producer", lambda base_url: FakeProducer())

    exit_code = cli.main(
        [
            "events",
            "send",
            "--type",
            "order.created",
            "--idempotency-key",
            "order-42-created",
            "--payload-file",
            str(payload),
            "--envelope-mode",
            "cloudevents",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["public_id"] == "event-1"
