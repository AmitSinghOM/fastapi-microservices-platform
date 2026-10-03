"""Safe recovery check (and optional replay) for one dead delivery.

Default is read-only: it fetches the advisory, independently rechecked
eligibility assessment and prints it. Replay only runs with --execute, and
if the assessment carries any warning, --acknowledge-warnings is also
required -- an operator decision, never an automatic one. This script does
not write, create, or infer any customer/environment mapping; project_id is
a trusted input the operator already knows out of band. On an ambiguous
(transport-level) failure it reports and exits non-zero without retrying.
"""

import argparse
import json
import os
import sys

from webhook_platform_sdk import ManagementClient
from webhook_platform_sdk.producer import ApiError, TransportError

base_url = os.environ["WEBHOOK_PLATFORM_URL"]
project_id = os.environ["RECOVERY_PROJECT_ID"]
api_key = os.environ["RECOVERY_MACHINE_API_KEY"]
delivery_id = os.environ["RECOVERY_DELIVERY_ID"]


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="replay the delivery if eligible; default is read-only",
    )
    parser.add_argument(
        "--acknowledge-warnings",
        action="store_true",
        help="required in addition to --execute when warnings are present",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = _args(argv)
    with ManagementClient(base_url, api_key=api_key) as management:
        try:
            recovery = management.get_delivery_recovery(
                project_id, delivery_id
            )
        except TransportError:
            print(
                "recovery check failed before a response; not retrying",
                file=sys.stderr,
            )
            return 1
        except ApiError as exc:
            print(f"recovery check failed: {exc}", file=sys.stderr)
            return 1

        print(json.dumps(recovery, indent=2, sort_keys=True))
        if not recovery["eligible_for_replay"]:
            print("not eligible for replay; blockers:", recovery["blockers"])
            return 0
        if not options.execute:
            print("eligible for replay; rerun with --execute to replay")
            return 0
        if recovery["warnings"] and not options.acknowledge_warnings:
            print(
                "warnings present; rerun with --execute "
                "--acknowledge-warnings to replay anyway:",
                recovery["warnings"],
                file=sys.stderr,
            )
            return 1

        try:
            result = management.replay_deliveries(
                project_id, [delivery_id], f"recovery-{delivery_id}"
            )
        except TransportError:
            print(
                "replay request failed before a response; not retrying -- "
                "rerun this command (same idempotency key) if needed",
                file=sys.stderr,
            )
            return 1
        except ApiError as exc:
            print(f"replay failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
