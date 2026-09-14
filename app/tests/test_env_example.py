"""`.env.example` must agree with the code's defaults where it states them.

Copying the example is the documented first step, and pydantic-settings
reads every key from the environment. On 2026-09-14 the example still said
`APP_VERSION=3.0.0` and `EXAMPLE_ITEMS_ENABLED=true` after the 4.0 flip, so
a fresh deployment would have reported the wrong version and re-enabled a
default-off API. This test pins the example to the code for the settings
whose *default* is part of the release contract.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]

# Keys whose value in .env.example must equal the code default, because the
# release policy makes promises about them. Everything else in the example
# is a documented starting point and may differ from the code default.
CONTRACT_KEYS = {
    "APP_VERSION": "app_version",
    "EXAMPLE_ITEMS_ENABLED": "example_items_enabled",
}


def _example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (
        (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    ):
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_env_example_matches_release_contract_defaults() -> None:
    example = _example_values()
    defaults = Settings(_env_file=None)
    for env_key, attr in CONTRACT_KEYS.items():
        assert env_key in example, f"{env_key} missing from .env.example"
        code_default = getattr(defaults, attr)
        stated = example[env_key]
        if isinstance(code_default, bool):
            assert stated.lower() == str(code_default).lower(), (
                f"{env_key}={stated} but code default is {code_default}"
            )
        else:
            assert stated == str(code_default), (
                f"{env_key}={stated} but code default is {code_default}"
            )


def test_env_example_loads_as_settings() -> None:
    """The example must be a valid settings file end to end."""
    settings = Settings(_env_file=ROOT / ".env.example")
    assert settings.app_version == Settings(_env_file=None).app_version
    assert settings.example_items_enabled is False
