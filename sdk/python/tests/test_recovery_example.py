"""Pure-SDK tests for examples/recovery/app.py (no FastAPI dependency)."""

import importlib.util
import sys
from pathlib import Path

import httpx

from webhook_platform_sdk.producer import ManagementClient

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[3] / "examples" / "recovery" / "app.py"
)

REQUIRED_ENV = {
    "WEBHOOK_PLATFORM_URL": "https://ignored.example",
    "RECOVERY_PROJECT_ID": "project-1",
    "RECOVERY_MACHINE_API_KEY": "whk_abc_machine",
    "RECOVERY_DELIVERY_ID": "delivery-1",
}


def _load_example(monkeypatch, env: dict[str, str] = REQUIRED_ENV):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        "recovery_example", EXAMPLE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wire(module, monkeypatch, handler):
    http_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    monkeypatch.setattr(
        module,
        "ManagementClient",
        lambda base_url, **kwargs: ManagementClient(
            base_url, api_key=kwargs["api_key"], client=http_client
        ),
    )


def test_default_is_read_only_even_when_eligible(monkeypatch, capsys) -> None:
    module = _load_example(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "delivery_id": "delivery-1",
                "status": "dead",
                "eligible_for_replay": True,
                "blockers": [],
                "warnings": [],
                "snapshot_preserved": True,
            },
        )

    _wire(module, monkeypatch, handler)

    exit_code = module.main([])

    assert exit_code == 0
    assert calls == ["/v1/projects/project-1/deliveries/delivery-1/recovery"]
    assert "--execute" in capsys.readouterr().out


def test_blockers_prevent_replay_even_with_execute(
    monkeypatch, capsys
) -> None:
    module = _load_example(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "delivery_id": "delivery-1",
                "status": "succeeded",
                "eligible_for_replay": False,
                "blockers": ["delivery_not_dead"],
                "warnings": [],
                "snapshot_preserved": True,
            },
        )

    _wire(module, monkeypatch, handler)

    exit_code = module.main(["--execute"])

    assert exit_code == 0
    assert "not eligible" in capsys.readouterr().out


def test_warnings_require_explicit_acknowledgement(
    monkeypatch, capsys
) -> None:
    module = _load_example(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "delivery_id": "delivery-1",
                "status": "dead",
                "eligible_for_replay": True,
                "blockers": [],
                "warnings": ["endpoint_configuration_changed"],
                "snapshot_preserved": True,
            },
        )

    _wire(module, monkeypatch, handler)

    exit_code = module.main(["--execute"])

    assert exit_code == 1
    assert calls == ["/v1/projects/project-1/deliveries/delivery-1/recovery"]
    assert "--acknowledge-warnings" in capsys.readouterr().err


def test_execute_with_acknowledged_warnings_replays(
    monkeypatch, capsys
) -> None:
    module = _load_example(monkeypatch)
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (request.url.path, request.headers.get("Idempotency-Key"))
        )
        if request.url.path.endswith("/recovery"):
            return httpx.Response(
                200,
                json={
                    "delivery_id": "delivery-1",
                    "status": "dead",
                    "eligible_for_replay": True,
                    "blockers": [],
                    "warnings": ["endpoint_paused"],
                    "snapshot_preserved": True,
                },
            )
        return httpx.Response(201, json={"operation_id": "replay-1"})

    _wire(module, monkeypatch, handler)

    exit_code = module.main(["--execute", "--acknowledge-warnings"])

    assert exit_code == 0
    assert calls[1] == (
        "/v1/projects/project-1/replays",
        "recovery-delivery-1",
    )
    assert "replay-1" in capsys.readouterr().out


def test_main_does_not_retry_on_transport_failure(monkeypatch, capsys) -> None:
    module = _load_example(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("boom")

    _wire(module, monkeypatch, handler)

    exit_code = module.main([])

    assert exit_code == 1
    assert calls == ["/v1/projects/project-1/deliveries/delivery-1/recovery"]
    assert "not retrying" in capsys.readouterr().err
