"""Record and evaluate Phase 8 usability evidence without participant PII."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

STUDY_VERSION = 1
TIME_LIMIT_SECONDS = 30 * 60
PARTICIPANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_RESULT_FIELDS = {
    "participant_id",
    "started_at",
    "completed_at",
    "duration_seconds",
    "independent",
    "maintainer_help",
    "signed_delivery_verified",
    "failed_step",
}


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def _validate_result(row: object) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != _RESULT_FIELDS:
        raise ValueError("study participant record is invalid")
    participant_id = row.get("participant_id")
    if not isinstance(participant_id, str) or not PARTICIPANT_ID.fullmatch(
        participant_id
    ):
        raise ValueError("study participant record is invalid")
    started_at = row.get("started_at")
    completed_at = row.get("completed_at")
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        raise ValueError("study participant record is invalid")
    started = _timestamp(started_at)
    completed = _timestamp(completed_at)
    expected_duration = (completed - started).total_seconds()
    duration = row.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or expected_duration < 0
        or abs(float(duration) - expected_duration) > 0.000_001
    ):
        raise ValueError("study participant duration is invalid")
    for field in (
        "independent",
        "maintainer_help",
        "signed_delivery_verified",
    ):
        if not isinstance(row.get(field), bool):
            raise ValueError("study participant record is invalid")
    failed_step = row.get("failed_step")
    if failed_step is not None and (
        not isinstance(failed_step, str) or len(failed_step) > 120
    ):
        raise ValueError("study participant record is invalid")
    return row


def _participants(study: dict[str, Any]) -> list[dict[str, Any]]:
    rows = study.get("participants")
    if study.get("version") != STUDY_VERSION or not isinstance(rows, list):
        raise ValueError("study document is invalid")
    participants = [_validate_result(row) for row in rows]
    identifiers = [row["participant_id"] for row in participants]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("study participant IDs must be unique")
    return participants


def new_study() -> dict[str, Any]:
    return {"version": STUDY_VERSION, "participants": []}


def record_result(
    study: dict[str, Any],
    *,
    participant_id: str,
    started_at: str,
    completed_at: str,
    independent: bool,
    maintainer_help: bool,
    signed_delivery_verified: bool,
    failed_step: str | None = None,
) -> None:
    if not PARTICIPANT_ID.fullmatch(participant_id):
        raise ValueError("participant_id must be a short pseudonymous ID")
    participants = _participants(study)
    if any(row["participant_id"] == participant_id for row in participants):
        raise ValueError("participant_id is already recorded")
    started = _timestamp(started_at)
    completed = _timestamp(completed_at)
    duration = (completed - started).total_seconds()
    if duration < 0:
        raise ValueError("completed_at must not precede started_at")
    if failed_step is not None and len(failed_step) > 120:
        raise ValueError("failed_step is too long")

    row = _validate_result(
        {
            "participant_id": participant_id,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_seconds": duration,
            "independent": independent,
            "maintainer_help": maintainer_help,
            "signed_delivery_verified": signed_delivery_verified,
            "failed_step": failed_step,
        }
    )
    participants.append(row)
    study["participants"] = participants


def evaluate(study: dict[str, Any]) -> dict[str, Any]:
    participants = _participants(study)
    eligible = [row for row in participants if row["independent"] is True]
    passed = [
        row
        for row in eligible
        if row["signed_delivery_verified"] is True
        and row["maintainer_help"] is False
        and row["duration_seconds"] < TIME_LIMIT_SECONDS
    ]
    return {
        "recorded_participants": len(participants),
        "eligible_independent_participants": len(eligible),
        "successful_under_30_minutes_without_help": len(passed),
        "required_independent_participants": 10,
        "required_successes": 8,
        "gate_passed": len(eligible) >= 10 and len(passed) >= 8,
    }


def _load(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("study file cannot be read safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("study file must be a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("study file permissions must not exceed 0600")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            document = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, dict):
        raise ValueError("study document must be an object")
    _participants(document)
    return document


def _write(path: Path, document: dict[str, Any], *, exclusive: bool) -> None:
    _participants(document)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("study_file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    record = commands.add_parser("record")
    record.add_argument("--participant-id", required=True)
    record.add_argument("--started-at", required=True)
    record.add_argument("--completed-at", required=True)
    record.add_argument(
        "--independent", action=argparse.BooleanOptionalAction, required=True
    )
    record.add_argument(
        "--maintainer-help",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    record.add_argument(
        "--signed-delivery-verified",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    record.add_argument("--failed-step")
    commands.add_parser("report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            _write(args.study_file, new_study(), exclusive=True)
            return 0
        study = _load(args.study_file)
        if args.command == "record":
            record_result(
                study,
                participant_id=args.participant_id,
                started_at=args.started_at,
                completed_at=args.completed_at,
                independent=args.independent,
                maintainer_help=args.maintainer_help,
                signed_delivery_verified=args.signed_delivery_verified,
                failed_step=args.failed_step,
            )
            _write(args.study_file, study, exclusive=False)
            return 0
        report = evaluate(study)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["gate_passed"] else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
