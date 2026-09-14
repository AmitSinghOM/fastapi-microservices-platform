"""Tests for scripts/bootstrap_env.py — idempotent secret bootstrap."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_env.py"
)
spec = importlib.util.spec_from_file_location("bootstrap_env", MODULE_PATH)
assert spec is not None and spec.loader is not None
bootstrap_env = importlib.util.module_from_spec(spec)
sys.modules["bootstrap_env"] = bootstrap_env
spec.loader.exec_module(bootstrap_env)

EXAMPLE = (
    "# comment\n"
    "APP_NAME=demo\n"
    "SECRET_KEY=\n"
    "API_KEY_PEPPER=\n"
    "WEBHOOK_SIGNING_KEY=\n"
    "ACCESS_TOKEN_EXPIRE_MINUTES=30\n"
)


def _values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_creates_env_and_fills_only_empty_secrets(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE)
    env = tmp_path / ".env"
    created, filled, kept = bootstrap_env.bootstrap(env, example)
    assert created is True
    assert filled == list(bootstrap_env.REQUIRED_SECRETS)
    assert kept == []
    values = _values(env)
    for key in bootstrap_env.REQUIRED_SECRETS:
        assert len(values[key]) >= 32
    assert values["APP_NAME"] == "demo"
    assert values["ACCESS_TOKEN_EXPIRE_MINUTES"] == "30"
    assert env.read_text().count("SECRET_KEY=") == 1


def test_second_run_changes_nothing(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE)
    env = tmp_path / ".env"
    bootstrap_env.bootstrap(env, example)
    before = env.read_text()
    created, filled, kept = bootstrap_env.bootstrap(env, example)
    assert (created, filled) == (False, [])
    assert kept == list(bootstrap_env.REQUIRED_SECRETS)
    assert env.read_text() == before


def test_existing_values_are_preserved(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(EXAMPLE.replace("SECRET_KEY=\n", "SECRET_KEY=keep-me\n"))
    created, filled, kept = bootstrap_env.bootstrap(env, tmp_path / "absent")
    assert created is False
    assert kept == ["SECRET_KEY"]
    assert filled == ["API_KEY_PEPPER", "WEBHOOK_SIGNING_KEY"]
    assert _values(env)["SECRET_KEY"] == "keep-me"


def test_missing_example_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = bootstrap_env.main(
        ["--env", str(tmp_path / ".env"), "--example", str(tmp_path / "nope")]
    )
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_duplicate_key_is_an_error(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(EXAMPLE + "SECRET_KEY=\n")
    with pytest.raises(bootstrap_env.BootstrapError):
        bootstrap_env.bootstrap(env, tmp_path / "unused")


def test_missing_required_key_is_an_error(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("APP_NAME=demo\nSECRET_KEY=\n")
    with pytest.raises(bootstrap_env.BootstrapError):
        bootstrap_env.bootstrap(env, tmp_path / "unused")


def test_cli_reports_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE)
    env = tmp_path / ".env"
    assert (
        bootstrap_env.main(["--env", str(env), "--example", str(example)]) == 0
    )
    first = capsys.readouterr().out
    assert "created" in first and "generated: SECRET_KEY" in first
    assert (
        bootstrap_env.main(["--env", str(env), "--example", str(example)]) == 0
    )
    assert "kept existing" in capsys.readouterr().out
