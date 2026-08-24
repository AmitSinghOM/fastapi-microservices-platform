"""`webhookctl` control-plane and test-event CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from webhook_platform_sdk.producer import (
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

    projects = commands.add_parser("projects")
    project_commands = projects.add_subparsers(dest="action", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("--organization", required=True)
    project_create = project_commands.add_parser("create")
    project_create.add_argument("--organization", required=True)
    project_create.add_argument("--name", required=True)

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


def _management(base_url: str) -> ManagementClient:
    token = os.getenv("WEBHOOK_PLATFORM_TOKEN")
    if not token:
        raise ValueError("WEBHOOK_PLATFORM_TOKEN is required")
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


def _management_command(args: argparse.Namespace) -> Any:
    with _management(args.base_url) as client:
        if args.resource == "projects":
            if args.action == "list":
                return client.list_projects(args.organization)
            return client.create_project(args.organization, args.name)
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
            return delivery.get("attempts", []) if args.action == "attempts" else delivery
        if args.resource == "replay":
            if args.action == "one":
                return client.replay_delivery(args.project, args.delivery)
            return client.replay_deliveries(
                args.project, args.delivery, args.idempotency_key
            )
    raise ValueError("unsupported command")


def _run(args: argparse.Namespace) -> Any:
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
    except (WebhookPlatformError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
