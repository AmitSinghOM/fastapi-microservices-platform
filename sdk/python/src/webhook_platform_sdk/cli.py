"""`webhookctl` control-plane and test-event CLI."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from webhook_platform_sdk.producer import (
    AuthClient,
    ManagementClient,
    Producer,
    WebhookPlatformError,
)


def _add_project_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webhookctl")
    parser.add_argument(
        "--base-url",
        default=os.getenv("WEBHOOK_PLATFORM_URL", "http://localhost:8000"),
    )
    commands = parser.add_subparsers(dest="resource", required=True)

    auth = commands.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="action", required=True)
    auth_register = auth_commands.add_parser("register")
    auth_register.add_argument("--email", required=True)
    auth_register.add_argument("--name", required=True)
    auth_login = auth_commands.add_parser("login")
    auth_login.add_argument("--email", required=True)

    organizations = commands.add_parser("organizations")
    organization_commands = organizations.add_subparsers(
        dest="action", required=True
    )
    organization_commands.add_parser("list")
    organization_create = organization_commands.add_parser("create")
    organization_create.add_argument("--name", required=True)

    projects = commands.add_parser("projects")
    project_commands = projects.add_subparsers(dest="action", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("--organization", required=True)
    project_create = project_commands.add_parser("create")
    project_create.add_argument("--organization", required=True)
    project_create.add_argument("--name", required=True)

    api_keys = commands.add_parser("api-keys")
    api_key_commands = api_keys.add_subparsers(dest="action", required=True)
    api_key_list = api_key_commands.add_parser("list")
    _add_project_id(api_key_list)
    api_key_create = api_key_commands.add_parser("create")
    _add_project_id(api_key_create)
    api_key_create.add_argument("--name", required=True)
    api_key_create.add_argument("--expires-in-days", type=int)
    api_key_revoke = api_key_commands.add_parser("revoke")
    _add_project_id(api_key_revoke)
    api_key_revoke.add_argument("--api-key", required=True)

    endpoints = commands.add_parser("endpoints")
    endpoint_commands = endpoints.add_subparsers(dest="action", required=True)
    endpoint_list = endpoint_commands.add_parser("list")
    _add_project_id(endpoint_list)
    endpoint_create = endpoint_commands.add_parser("create")
    _add_project_id(endpoint_create)
    endpoint_create.add_argument("--url", required=True)
    endpoint_create.add_argument("--description")

    events = commands.add_parser("events")
    event_commands = events.add_subparsers(dest="action", required=True)
    event_send = event_commands.add_parser("send")
    event_send.add_argument("--type", required=True)
    event_send.add_argument("--idempotency-key", required=True)
    event_send.add_argument("--payload-file", required=True)
    event_send.add_argument(
        "--envelope-mode", choices=("native", "cloudevents"), default="native"
    )

    deliveries = commands.add_parser("deliveries")
    delivery_commands = deliveries.add_subparsers(dest="action", required=True)
    delivery_list = delivery_commands.add_parser("list")
    _add_project_id(delivery_list)
    delivery_list.add_argument("--offset", type=int, default=0)
    delivery_list.add_argument("--limit", type=int, default=50)
    for action in ("get", "attempts"):
        command = delivery_commands.add_parser(action)
        _add_project_id(command)
        command.add_argument("--delivery", required=True)

    replay = commands.add_parser("replay")
    replay_commands = replay.add_subparsers(dest="action", required=True)
    replay_one = replay_commands.add_parser("one")
    _add_project_id(replay_one)
    replay_one.add_argument("--delivery", required=True)
    replay_bulk = replay_commands.add_parser("bulk")
    _add_project_id(replay_bulk)
    replay_bulk.add_argument("--delivery", action="append", required=True)
    replay_bulk.add_argument("--idempotency-key", required=True)
    return parser


def _credentials_path() -> Path:
    configured = os.getenv("WEBHOOK_PLATFORM_CREDENTIALS")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config/webhook-platform/credentials.json"


def _save_credentials(base_url: str, token: str) -> Path:
    path = _credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(
                {"base_url": base_url.rstrip("/"), "token": token},
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path


def _stored_token(base_url: str) -> str | None:
    path = _credentials_path()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("credential file cannot be read safely") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("credential file must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("credential file permissions must be 0600")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            document = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not isinstance(document, dict):
        raise ValueError("credential file is invalid")
    stored_url = document.get("base_url")
    token = document.get("token")
    if stored_url != base_url.rstrip("/"):
        raise ValueError("credential file belongs to a different base URL")
    if not isinstance(token, str) or not token:
        raise ValueError("credential file is invalid")
    return token


def _management(base_url: str) -> ManagementClient:
    token = os.getenv("WEBHOOK_PLATFORM_TOKEN") or _stored_token(base_url)
    if not token:
        raise ValueError(
            "run `webhookctl auth login` or set WEBHOOK_PLATFORM_TOKEN"
        )
    return ManagementClient(base_url, token)


def _producer(base_url: str) -> Producer:
    api_key = os.getenv("WEBHOOK_PLATFORM_API_KEY")
    if not api_key:
        raise ValueError("WEBHOOK_PLATFORM_API_KEY is required")
    return Producer(base_url, api_key)


def _payload(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _auth_command(args: argparse.Namespace) -> dict[str, Any]:
    password = getpass("Password: ")
    if args.action == "register":
        confirmation = getpass("Confirm password: ")
        if password != confirmation:
            raise ValueError("passwords do not match")
        with AuthClient(args.base_url) as client:
            return client.register(args.email, args.name, password)

    with AuthClient(args.base_url) as client:
        token = client.login(args.email, password)
    path = _save_credentials(args.base_url, token)
    return {"status": "stored", "credential_file": str(path)}


def _management_command(args: argparse.Namespace) -> Any:
    with _management(args.base_url) as client:
        if args.resource == "organizations":
            if args.action == "list":
                return client.list_organizations()
            return client.create_organization(args.name)
        if args.resource == "projects":
            if args.action == "list":
                return client.list_projects(args.organization)
            return client.create_project(args.organization, args.name)
        if args.resource == "api-keys":
            if args.action == "list":
                return client.list_api_keys(args.project)
            if args.action == "create":
                return client.create_api_key(
                    args.project,
                    args.name,
                    expires_in_days=args.expires_in_days,
                )
            return client.revoke_api_key(args.project, args.api_key)
        if args.resource == "endpoints":
            if args.action == "list":
                return client.list_endpoints(args.project)
            return client.create_endpoint(
                args.project, args.url, args.description
            )
        if args.resource == "deliveries":
            if args.action == "list":
                return client.list_deliveries(
                    args.project, offset=args.offset, limit=args.limit
                )
            delivery = client.get_delivery(args.project, args.delivery)
            if args.action == "attempts":
                return delivery.get("attempts", [])
            return delivery
        if args.resource == "replay":
            if args.action == "one":
                return client.replay_delivery(args.project, args.delivery)
            return client.replay_deliveries(
                args.project, args.delivery, args.idempotency_key
            )
    raise ValueError("unsupported command")


def _run(args: argparse.Namespace) -> Any:
    if args.resource == "auth":
        return _auth_command(args)
    if args.resource == "events":
        with _producer(args.base_url) as producer:
            return producer.send_event(
                args.type,
                _payload(args.payload_file),
                args.idempotency_key,
                envelope_mode=args.envelope_mode,
            )
    return _management_command(args)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except (
        WebhookPlatformError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
