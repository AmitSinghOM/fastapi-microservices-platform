from datetime import datetime, timedelta, timezone

import pytest

from scripts.phase8_usability_study import (
    TIME_LIMIT_SECONDS,
    evaluate,
    main,
    new_study,
    record_result,
)


def iso(value: datetime) -> str:
    return value.isoformat()


def test_gate_requires_ten_independent_people_and_eight_successes() -> None:
    study = new_study()
    started = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
    for number in range(10):
        duration = timedelta(minutes=20 if number < 8 else 35)
        record_result(
            study,
            participant_id=f"participant-{number + 1}",
            started_at=iso(started),
            completed_at=iso(started + duration),
            independent=True,
            maintainer_help=False,
            signed_delivery_verified=True,
        )

    report = evaluate(study)
    assert report["eligible_independent_participants"] == 10
    assert report["successful_under_30_minutes_without_help"] == 8
    assert report["gate_passed"] is True


def test_exactly_thirty_minutes_or_maintainer_help_does_not_pass() -> None:
    study = new_study()
    started = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
    record_result(
        study,
        participant_id="participant-boundary",
        started_at=iso(started),
        completed_at=iso(started + timedelta(seconds=TIME_LIMIT_SECONDS)),
        independent=True,
        maintainer_help=True,
        signed_delivery_verified=True,
    )

    assert evaluate(study)["successful_under_30_minutes_without_help"] == 0


def test_rejects_duplicate_or_naive_participant_records() -> None:
    study = new_study()
    values = {
        "participant_id": "participant-1",
        "started_at": "2026-08-24T10:00:00+00:00",
        "completed_at": "2026-08-24T10:10:00+00:00",
        "independent": True,
        "maintainer_help": False,
        "signed_delivery_verified": True,
    }
    record_result(study, **values)
    with pytest.raises(ValueError, match="already recorded"):
        record_result(study, **values)

    values["participant_id"] = "participant-2"
    values["started_at"] = "2026-08-24T10:00:00"
    with pytest.raises(ValueError, match="timezone"):
        record_result(study, **values)


def test_rejects_tampered_duration_or_malformed_participant() -> None:
    study = new_study()
    record_result(
        study,
        participant_id="participant-1",
        started_at="2026-08-24T10:00:00+00:00",
        completed_at="2026-08-24T10:10:00+00:00",
        independent=True,
        maintainer_help=False,
        signed_delivery_verified=True,
    )
    study["participants"][0]["duration_seconds"] = 1
    with pytest.raises(ValueError, match="duration is invalid"):
        evaluate(study)

    study["participants"] = ["not-an-object"]
    with pytest.raises(ValueError, match="record is invalid"):
        evaluate(study)


def test_cli_uses_secure_atomic_study_file(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    study_file = tmp_path / "study.json"
    assert main([str(study_file), "init"]) == 0
    assert study_file.stat().st_mode & 0o777 == 0o600

    record = [
        str(study_file),
        "record",
        "--participant-id",
        "participant-1",
        "--started-at",
        "2026-08-24T10:00:00Z",
        "--completed-at",
        "2026-08-24T10:10:00Z",
        "--independent",
        "--no-maintainer-help",
        "--signed-delivery-verified",
    ]
    assert main(record) == 0
    persisted = study_file.read_bytes()

    assert main(record) == 2
    assert study_file.read_bytes() == persisted
    assert "already recorded" in capsys.readouterr().err
