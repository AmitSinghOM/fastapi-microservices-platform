import json

import pytest

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


class FakeAuthClient:
    def __init__(self, base_url):
        assert base_url == "https://api.example"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def login(self, email, password):
        assert email == "owner@example.com"
        assert password == "password-value"
        return "jwt-token-value"


def test_login_stores_token_without_printing_it(
    monkeypatch, tmp_path, capsys
) -> None:
    credentials = tmp_path / "private" / "credentials.json"
    monkeypatch.setenv("WEBHOOK_PLATFORM_CREDENTIALS", str(credentials))
    monkeypatch.setattr(cli, "AuthClient", FakeAuthClient)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "password-value")

    exit_code = cli.main(
        [
            "--base-url",
            "https://api.example",
            "auth",
            "login",
            "--email",
            "owner@example.com",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "jwt-token-value" not in output
    assert credentials.parent.stat().st_mode & 0o777 == 0o700
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert cli._stored_token("https://api.example") == "jwt-token-value"


def test_registration_password_mismatch_never_constructs_client(
    monkeypatch, capsys
) -> None:
    passwords = iter(("first-password", "different-password"))
    monkeypatch.setattr(cli, "getpass", lambda prompt: next(passwords))

    def unexpected_client(base_url):
        raise AssertionError("AuthClient must not be constructed")

    monkeypatch.setattr(cli, "AuthClient", unexpected_client)
    exit_code = cli.main(
        [
            "auth",
            "register",
            "--email",
            "owner@example.com",
            "--name",
            "Owner",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "passwords do not match" in captured.err
    assert "first-password" not in captured.err
    assert "different-password" not in captured.err


def test_stored_token_rejects_non_object_document(
    monkeypatch, tmp_path
) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text("[]", encoding="utf-8")
    credentials.chmod(0o600)
    monkeypatch.setenv("WEBHOOK_PLATFORM_CREDENTIALS", str(credentials))

    with pytest.raises(ValueError, match="credential file is invalid"):
        cli._stored_token("https://api.example")


class FakeManagementClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def create_organization(self, name):
        self.calls.append(("organization-create", name))
        return {"public_id": "org-1"}

    def list_api_keys(self, project_id):
        self.calls.append(("api-key-list", project_id))
        return []

    def create_api_key(self, project_id, name, *, expires_in_days=None):
        self.calls.append(
            ("api-key-create", project_id, name, expires_in_days)
        )
        return {"public_id": "key-1", "plaintext_key": "one-time-key"}

    def revoke_api_key(self, project_id, api_key_id):
        self.calls.append(("api-key-revoke", project_id, api_key_id))
        return {"public_id": api_key_id, "revoked_at": "now"}


def test_organization_and_api_key_commands_dispatch(monkeypatch, capsys) -> None:
    management = FakeManagementClient()
    monkeypatch.setattr(cli, "_management", lambda base_url: management)

    commands = [
        ["organizations", "create", "--name", "Example"],
        ["api-keys", "list", "--project", "project-1"],
        [
            "api-keys",
            "create",
            "--project",
            "project-1",
            "--name",
            "producer",
            "--expires-in-days",
            "30",
        ],
        [
            "api-keys",
            "revoke",
            "--project",
            "project-1",
            "--api-key",
            "key-1",
        ],
    ]
    for command in commands:
        assert cli.main(command) == 0
        capsys.readouterr()

    assert management.calls == [
        ("organization-create", "Example"),
        ("api-key-list", "project-1"),
        ("api-key-create", "project-1", "producer", 30),
        ("api-key-revoke", "project-1", "key-1"),
    ]
