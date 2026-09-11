"""Endpoint event-type subscription support in the SDK and CLI."""

import json

import httpx
import pytest

from webhook_platform_sdk import cli
from webhook_platform_sdk.producer import ManagementClient


def _management(handler) -> ManagementClient:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    return ManagementClient("https://ignored.example", "token", client=client)


def test_create_endpoint_omits_event_types_when_unset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"public_id": "ep-1"})

    management = _management(handler)
    management.create_endpoint("proj-1", "https://receiver.example/hook")

    body = json.loads(requests[0].content)
    assert "event_types" not in body
    assert body["url"] == "https://receiver.example/hook"


def test_create_endpoint_sends_event_types() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"public_id": "ep-1"})

    management = _management(handler)
    management.create_endpoint(
        "proj-1",
        "https://receiver.example/hook",
        event_types=["order.*", "user.created"],
    )

    body = json.loads(requests[0].content)
    assert body["event_types"] == ["order.*", "user.created"]


def test_update_endpoint_sends_only_passed_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"public_id": "ep-1"})

    management = _management(handler)
    management.update_endpoint("proj-1", "ep-1", event_types=["invoice.paid"])

    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1/projects/proj-1/endpoints/ep-1"
    assert json.loads(requests[0].content) == {"event_types": ["invoice.paid"]}


def test_update_endpoint_explicit_none_clears_filter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"public_id": "ep-1"})

    management = _management(handler)
    management.update_endpoint("proj-1", "ep-1", event_types=None)

    assert json.loads(requests[0].content) == {"event_types": None}


def test_update_endpoint_requires_a_change() -> None:
    management = _management(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        management.update_endpoint("proj-1", "ep-1")


class FakeManagement:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def create_endpoint(
        self,
        project_id,
        url,
        description,
        *,
        event_types,
        signature_scheme=None,
    ):
        self.calls.append(
            ("create", project_id, url, description, event_types)
        )
        return {"public_id": "ep-1", "event_types": event_types}

    def update_endpoint(self, project_id, endpoint_id, **changes):
        self.calls.append(("update", project_id, endpoint_id, changes))
        return {"public_id": endpoint_id, **changes}


def test_cli_create_with_repeatable_event_type(monkeypatch, capsys) -> None:
    fake = FakeManagement()
    monkeypatch.setattr(cli, "_management", lambda base_url: fake)

    exit_code = cli.main(
        [
            "endpoints",
            "create",
            "--project",
            "proj-1",
            "--url",
            "https://receiver.example/hook",
            "--event-type",
            "order.*",
            "--event-type",
            "user.created",
        ]
    )

    assert exit_code == 0
    assert fake.calls == [
        (
            "create",
            "proj-1",
            "https://receiver.example/hook",
            None,
            ["order.*", "user.created"],
        )
    ]
    assert json.loads(capsys.readouterr().out)["event_types"] == [
        "order.*",
        "user.created",
    ]


def test_cli_update_replaces_filter(monkeypatch, capsys) -> None:
    fake = FakeManagement()
    monkeypatch.setattr(cli, "_management", lambda base_url: fake)

    exit_code = cli.main(
        [
            "endpoints",
            "update",
            "--project",
            "proj-1",
            "--endpoint",
            "ep-1",
            "--event-type",
            "invoice.paid",
        ]
    )

    assert exit_code == 0
    assert fake.calls == [
        ("update", "proj-1", "ep-1", {"event_types": ["invoice.paid"]})
    ]


def test_cli_update_all_events_clears_filter(monkeypatch) -> None:
    fake = FakeManagement()
    monkeypatch.setattr(cli, "_management", lambda base_url: fake)

    exit_code = cli.main(
        [
            "endpoints",
            "update",
            "--project",
            "proj-1",
            "--endpoint",
            "ep-1",
            "--all-events",
        ]
    )

    assert exit_code == 0
    assert fake.calls == [("update", "proj-1", "ep-1", {"event_types": None})]


def test_cli_update_without_changes_errors(monkeypatch, capsys) -> None:
    fake = FakeManagement()
    monkeypatch.setattr(cli, "_management", lambda base_url: fake)

    exit_code = cli.main(
        ["endpoints", "update", "--project", "proj-1", "--endpoint", "ep-1"]
    )

    assert exit_code == 2
    assert fake.calls == []
    assert "at least one change" in capsys.readouterr().err


def test_cli_update_rejects_filter_conflict(monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "endpoints",
                "update",
                "--project",
                "proj-1",
                "--endpoint",
                "ep-1",
                "--event-type",
                "a.b",
                "--all-events",
            ]
        )
    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err
