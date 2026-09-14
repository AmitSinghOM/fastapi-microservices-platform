"""Create `.env` from `.env.example` and fill the three shared secrets once.

Replaces the README's shell one-liner, which appended fresh values every
time it ran and thereby rotated the signing key underneath existing
endpoint secrets. This tool is idempotent:

- if `.env` does not exist, it is copied from `.env.example`;
- each of SECRET_KEY, API_KEY_PEPPER, WEBHOOK_SIGNING_KEY is filled with a
  fresh 32-byte URL-safe token **only if its value is empty**;
- non-empty values are never changed, and nothing is appended, so running
  it again is a no-op and prints which keys were left untouched.

Exit status is 0 when `.env` is ready (created, filled, or already
complete) and 2 on a malformed file or missing `.env.example`.
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from pathlib import Path

REQUIRED_SECRETS = ("SECRET_KEY", "API_KEY_PEPPER", "WEBHOOK_SIGNING_KEY")
TOKEN_BYTES = 32


class BootstrapError(ValueError):
    """Raised when `.env` cannot be prepared safely."""


def _fill_secrets(text: str) -> tuple[str, list[str], list[str]]:
    """Return (new_text, filled_keys, kept_keys)."""
    lines = text.splitlines(keepends=True)
    seen: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in REQUIRED_SECRETS:
            if key in seen:
                raise BootstrapError(f"{key} appears more than once in .env")
            seen[key] = index
    missing = [key for key in REQUIRED_SECRETS if key not in seen]
    if missing:
        raise BootstrapError(
            "missing required keys in .env: " + ", ".join(missing)
        )
    filled: list[str] = []
    kept: list[str] = []
    for key in REQUIRED_SECRETS:
        index = seen[key]
        line = lines[index]
        value = line.split("=", 1)[1].strip()
        if value:
            kept.append(key)
            continue
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{key}={secrets.token_urlsafe(TOKEN_BYTES)}{newline}"
        filled.append(key)
    return "".join(lines), filled, kept


def bootstrap(
    env_path: Path, example_path: Path
) -> tuple[bool, list[str], list[str]]:
    """Prepare `env_path`. Returns (created, filled_keys, kept_keys)."""
    created = False
    if not env_path.exists():
        if not example_path.exists():
            raise BootstrapError(f"{example_path} not found")
        shutil.copyfile(example_path, env_path)
        created = True
    text = env_path.read_text(encoding="utf-8")
    new_text, filled, kept = _fill_secrets(text)
    if new_text != text:
        temp = env_path.with_name(env_path.name + ".tmp")
        temp.write_text(new_text, encoding="utf-8")
        temp.replace(env_path)
    return created, filled, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--example", type=Path, default=Path(".env.example"))
    args = parser.parse_args(argv)
    try:
        created, filled, kept = bootstrap(args.env, args.example)
    except (BootstrapError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if created:
        print(f"created {args.env} from {args.example}")
    if filled:
        print("generated: " + ", ".join(filled))
    if kept:
        print("kept existing: " + ", ".join(kept))
    if not created and not filled:
        print(f"{args.env} already complete; nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
