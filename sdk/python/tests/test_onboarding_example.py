"""Pure-SDK tests for examples/onboarding/app.py (no FastAPI dependency)."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from webhook_platform_sdk.producer import ManagementClient

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[3] / "examples" / "onboarding" / "app.py"
)

REQUIRED_ENV = {
    "WEBHOOK_PLATFORM_URL": "https://ignored.example",
    "ONBOARDING_ORGANIZATION_ID": "org-1",
    "ONBOARDING_PROJECT_NAME": "Events",
    "ONBOARDING_ENDPOINT_URL": "https://receiver.internal.example/webhooks",
    "ONBOARDING_JWT_TOKEN": "jwt-token",
}


def _load_example(monkeypatch, tmp_path, env: dict[str, str] = REQUIRED_ENV):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ONBOARDING_STATE_DIR", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location(
        f"onboarding_example_{id(tmp_path)}", EXAMPLE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _happy_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/projects"):
        return httpx.Response(201, json={"public_id": "project-1"})
    if path.endswith("/endpoints"):
        return httpx.Response(
            201, json={"public_id": "endpoint-1", "signing_secret": "whsec_a"}
        )
    if path.endswith("/api-keys"):
        name = json.loads(request.content)["name"]
        return httpx.Response(
            201,
            json={"public_id": f"key-{name}", "plaintext_key": f"whk_{name}"},
        )
    raise AssertionError(f"unexpected path {path}")


def _wire(module, monkeypatch, handler):
    http_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.example"
    )
    monkeypatch.setattr(
        module,
        "ManagementClient",
        lambda base_url, token, **kwargs: ManagementClient(
            base_url, token, client=http_client
        ),
    )


def test_full_run_checkpoints_and_writes_mode_0600_secrets(
    monkeypatch, tmp_path
) -> None:
    module = _load_example(monkeypatch, tmp_path)
    _wire(module, monkeypatch, _happy_handler)

    exit_code = module.main()

    assert exit_code == 0
    checkpoint = json.loads(module.checkpoint_path.read_text(encoding="utf-8"))
    assert set(checkpoint) == {
        "_binding",
        "project",
        "endpoint",
        "producer_api_key",
        "machine_api_key",
    }
    assert "receiver.internal.example" not in json.dumps(checkpoint)
    for name in ("project", "endpoint", "producer_api_key", "machine_api_key"):
        secret_path = module.state_dir / f"{name}.secret"
        if secret_path.exists():
            assert secret_path.stat().st_mode & 0o777 == 0o600


def test_rerun_after_full_success_skips_every_step(
    monkeypatch, tmp_path
) -> None:
    module = _load_example(monkeypatch, tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _happy_handler(request)

    _wire(module, monkeypatch, handler)
    assert module.main() == 0
    calls.clear()

    assert module.main() == 0
    assert calls == []


def test_transport_failure_mid_run_blocks_rerun_of_that_step(
    monkeypatch, tmp_path
) -> None:
    module = _load_example(monkeypatch, tmp_path)
    calls: list[str] = []

    def failing_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/endpoints"):
            raise httpx.ConnectError("boom")
        return _happy_handler(request)

    _wire(module, monkeypatch, failing_handler)
    with pytest.raises(SystemExit):
        module.main()
    checkpoint = module._load_checkpoint()
    assert "project" in checkpoint
    assert "endpoint" not in checkpoint
    assert (module.state_dir / "endpoint.pending").exists()
    calls.clear()

    # Ambiguous failure means the server may already have the resource:
    # a rerun must not call the endpoint-create route again.
    _wire(module, monkeypatch, failing_handler)
    with pytest.raises(SystemExit):
        module.main()
    assert calls == []


def test_orphan_secret_blocks_before_endpoint_mutation(
    monkeypatch, tmp_path
) -> None:
    module = _load_example(monkeypatch, tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _happy_handler(request)

    _wire(module, monkeypatch, handler)
    module.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    module._write_secret_once("endpoint", "whsec_stray")

    with pytest.raises(SystemExit):
        module.main()

    assert all(not path.endswith("/endpoints") for path in calls)


def test_rebinding_a_dirty_checkpoint_without_binding_is_rejected(
    monkeypatch, tmp_path
) -> None:
    module = _load_example(monkeypatch, tmp_path)
    module.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    module._save_checkpoint({"project": {"public_id": "stale-project"}})

    _wire(module, monkeypatch, _happy_handler)
    with pytest.raises(SystemExit):
        module.main()


def test_changed_inputs_are_rejected_before_any_request(
    monkeypatch, tmp_path
) -> None:
    module = _load_example(monkeypatch, tmp_path)
    _wire(module, monkeypatch, _happy_handler)
    assert module.main() == 0

    other_env = dict(REQUIRED_ENV)
    other_env["ONBOARDING_PROJECT_NAME"] = "Different Project"
    other = _load_example(monkeypatch, tmp_path, env=other_env)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _happy_handler(request)

    _wire(other, monkeypatch, handler)
    with pytest.raises(SystemExit):
        other.main()
    assert calls == []


@pytest.mark.parametrize("failure", ["checkpoint", "secret"])
def test_persistence_failure_blocks_repeating_accepted_mutation(
    monkeypatch, tmp_path, failure
):
    module = _load_example(monkeypatch, tmp_path)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return _happy_handler(request)

    _wire(module, monkeypatch, handler)
    original_save = module._save_checkpoint

    def fail_checkpoint(checkpoint):
        if "project" in checkpoint:
            raise OSError("simulated disk failure")
        original_save(checkpoint)

    def fail_secret(name, value):
        raise OSError("simulated disk failure")

    if failure == "checkpoint":
        monkeypatch.setattr(module, "_save_checkpoint", fail_checkpoint)
        expected = OSError
        step = "project"
    else:
        monkeypatch.setattr(module, "_write_secret_once", fail_secret)
        expected = SystemExit
        step = "endpoint"
    with pytest.raises(expected):
        module.main()
    assert (module.state_dir / f"{step}.pending").exists()
    assert step not in module._load_checkpoint()
    calls.clear()
    with pytest.raises(SystemExit):
        module.main()
    assert calls == []
