"""Reject manifest drift and unhashed deployment requirements without network access."""

from __future__ import annotations

import re
from pathlib import Path

PIN = re.compile(
    r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+!-]+)"
)
HASH = re.compile(r"--hash=sha256:[a-f0-9]{64}")


def read_pins(text: str, *, hashed: bool) -> dict[str, str]:
    pins = {}
    logical = text.replace("\\\n", " ")
    for line in logical.splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        match = PIN.fullmatch(parts[0])
        if match is None:
            raise ValueError(f"Expected exact package pin: {parts[0]}")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in pins:
            raise ValueError(f"Duplicate package: {name}")
        if hashed:
            if len(parts) < 2 or not all(HASH.fullmatch(p) for p in parts[1:]):
                raise ValueError(f"Missing or invalid SHA256 hashes: {name}")
        elif len(parts) != 1:
            raise ValueError(f"Unexpected manifest options: {name}")
        pins[name] = match[2]
    if not pins:
        raise ValueError("Empty requirements")
    return pins


def check(manifest: str, lock: str) -> int:
    direct = read_pins(manifest, hashed=False)
    resolved = read_pins(lock, hashed=True)
    for name, version in direct.items():
        if resolved.get(name) != version:
            raise ValueError(f"Deployment lock drift: {name}=={version}")
    return len(resolved)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        count = check(
            (root / "requirements.txt").read_text(),
            (root / "requirements-deploy.txt").read_text(),
        )
    except (OSError, ValueError) as error:
        print(error)
        return 1
    print(f"Deployment lock: {count} hashed pins; direct versions match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
