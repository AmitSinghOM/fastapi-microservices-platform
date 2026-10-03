"""Safe, resumable onboarding: project + endpoint + two API keys.

Trusted inputs only (an existing organization, a chosen project name, and
the receiver endpoint URL) -- this script never infers routing from
request content. The checkpoint directory is bound, on first use, to a
fingerprint of those inputs (the endpoint URL is hashed, never stored or
printed in plaintext, since it may embed a secret query parameter);
reusing the directory for different inputs -- or finding step entries with
no recorded fingerprint at all -- aborts before any request rather than
silently adopting IDs that may belong to a different target.

Every step preflights for an orphaned secret file, then writes a durable
(fsynced, create-only) ``.pending`` marker *before* issuing its request,
and only removes that marker after the step's result -- and one-time
secret, if any -- is fully and durably recorded. A transport failure, a
clean API error, or a local write failure all leave the marker in place,
so a rerun never repeats a step whose request may have already reached
the server; it aborts with reconciliation instructions instead. This
fail-closed marker assumes a single operator running the script
sequentially; it is not a concurrency-safe lock against two simultaneous
runs against the same state directory.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

from webhook_platform_sdk import ManagementClient
from webhook_platform_sdk.producer import ApiError, TransportError

base_url = os.environ["WEBHOOK_PLATFORM_URL"]
organization_id = os.environ["ONBOARDING_ORGANIZATION_ID"]
project_name = os.environ["ONBOARDING_PROJECT_NAME"]
endpoint_url = os.environ["ONBOARDING_ENDPOINT_URL"]
token = os.environ["ONBOARDING_JWT_TOKEN"]

state_dir = Path(os.getenv("ONBOARDING_STATE_DIR", "onboarding_state"))
checkpoint_path = state_dir / "checkpoint.json"
_binding = {
    "base_url": base_url,
    "organization_id": organization_id,
    "project_name": project_name,
    "endpoint_url_sha256": hashlib.sha256(
        endpoint_url.encode("utf-8")
    ).hexdigest(),
}


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_checkpoint() -> dict[str, dict[str, str]]:
    if not checkpoint_path.exists():
        return {}
    return json.loads(checkpoint_path.read_text(encoding="utf-8"))


def _save_checkpoint(checkpoint: dict[str, dict[str, str]]) -> None:
    """Atomic, mode-0600, fsynced write; public IDs/fingerprint only."""
    tmp = checkpoint_path.with_suffix(".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(checkpoint, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, checkpoint_path)
    os.chmod(checkpoint_path, 0o600)
    _fsync_dir(state_dir)


def _write_secret_once(name: str, secret: str) -> None:
    """Create-only (never overwrite), fsynced, mode-0600 secret file."""
    path = state_dir / f"{name}.secret"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(state_dir)


def _create_pending_marker(path: Path) -> None:
    """Fsynced, create-only marker; durable before the request is sent."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_dir(state_dir)


def _reconcile(name: str, message: str) -> None:
    print(f"[{name}] RECONCILIATION REQUIRED: {message}", file=sys.stderr)
    raise SystemExit(1)


def _check_binding(checkpoint: dict[str, dict[str, str]]) -> None:
    if "_binding" in checkpoint:
        if checkpoint["_binding"] != _binding:
            _reconcile(
                "_binding",
                f"{state_dir} is bound to different onboarding inputs; use "
                "a fresh ONBOARDING_STATE_DIR for a different customer, "
                "environment, project, or endpoint rather than reusing "
                "this one.",
            )
        return
    if checkpoint:
        _reconcile(
            "_binding",
            f"{state_dir} has step entries but no recorded input "
            "fingerprint; refusing to adopt them for the current inputs "
            "without manual confirmation.",
        )
    checkpoint["_binding"] = _binding
    _save_checkpoint(checkpoint)


def _run_step(
    checkpoint: dict[str, dict[str, str]],
    name: str,
    mutate,
    secret_field: str | None,
) -> None:
    if name in checkpoint:
        print(f"[{name}] already completed, skipping", file=sys.stderr)
        return
    pending_path = state_dir / f"{name}.pending"
    secret_path = state_dir / f"{name}.secret" if secret_field else None
    if pending_path.exists():
        _reconcile(
            name,
            "a pending marker exists with no checkpoint entry -- this "
            "step's request may have already reached the server. Verify "
            "server state manually, then either record its public_id in "
            f"the checkpoint or remove {pending_path} once confirmed safe.",
        )
    if secret_path is not None and secret_path.exists():
        _reconcile(
            name,
            f"{secret_path} exists with no checkpoint entry -- a prior "
            "run's secret for this step was written without being "
            "confirmed. Do not rerun; reconcile that secret/resource out "
            "of band first.",
        )
    # Durable marker written *before* the request: any failure from here
    # on (transport, API, or local write) leaves it in place.
    _create_pending_marker(pending_path)
    try:
        result = mutate()
    except TransportError:
        _reconcile(name, "the request failed before a response; not retrying")
    except ApiError as exc:
        _reconcile(name, f"the request failed: {exc}")

    if secret_field is not None:
        try:
            _write_secret_once(name, result[secret_field])
        except OSError as exc:
            _reconcile(
                name,
                f"the server accepted this request "
                f"(public_id={result.get('public_id')!r}) but its "
                f"one-time secret could not be written locally ({exc}); "
                "capture or rotate that secret out of band instead of "
                "rerunning this step.",
            )
    checkpoint[name] = {"public_id": result["public_id"]}
    _save_checkpoint(checkpoint)
    pending_path.unlink()
    print(f"[{name}] completed: {result['public_id']}")


def main() -> int:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _fsync_dir(state_dir.parent)
    checkpoint = _load_checkpoint()
    _check_binding(checkpoint)

    with ManagementClient(base_url, token) as management:
        _run_step(
            checkpoint,
            "project",
            lambda: management.create_project(organization_id, project_name),
            secret_field=None,
        )
        project_id = checkpoint["project"]["public_id"]

        _run_step(
            checkpoint,
            "endpoint",
            lambda: management.create_endpoint(project_id, endpoint_url),
            secret_field="signing_secret",
        )
        _run_step(
            checkpoint,
            "producer_api_key",
            lambda: management.create_api_key(project_id, "producer"),
            secret_field="plaintext_key",
        )
        _run_step(
            checkpoint,
            "machine_api_key",
            lambda: management.create_api_key(
                project_id,
                "machine",
                scopes=["deliveries:read", "deliveries:replay"],
            ),
            secret_field="plaintext_key",
        )
    print(f"onboarding complete; secrets are under {state_dir}/*.secret")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
