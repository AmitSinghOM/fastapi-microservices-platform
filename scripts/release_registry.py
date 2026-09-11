"""Verify and recover Python package-index publication safely."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen

PROJECT = "fastapi-microservices-platform-sdk"
DISTRIBUTION_STEM = "fastapi_microservices_platform_sdk"
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
# Sidecar written next to each distribution by pypa/gh-action-pypi-publish.
ATTESTATION_SUFFIX = ".publish.attestation"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Registry:
    api_base: str
    artifact_host: str


REGISTRIES = {
    "testpypi": Registry(
        api_base="https://test.pypi.org/pypi",
        artifact_host="test-files.pythonhosted.org",
    ),
    "pypi": Registry(
        api_base="https://pypi.org/pypi",
        artifact_host="files.pythonhosted.org",
    ),
}


class ReleaseVerificationError(ValueError):
    """The registry state does not match the approved release."""


def _expected_names(version: str) -> set[str]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ReleaseVerificationError("version must be MAJOR.MINOR.PATCH")
    stem = f"{DISTRIBUTION_STEM}-{version}"
    return {f"{stem}.tar.gz", f"{stem}-py3-none-any.whl"}


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_manifest(path: Path, version: str) -> dict[str, str]:
    expected = _expected_names(version)
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise ReleaseVerificationError("SHA256SUMS has an invalid row")
        digest, filename = parts
        if not _SHA256.fullmatch(digest) or Path(filename).name != filename:
            raise ReleaseVerificationError("SHA256SUMS has an invalid row")
        if filename in rows:
            raise ReleaseVerificationError("SHA256SUMS repeats a filename")
        rows[filename] = digest
    if set(rows) != expected:
        raise ReleaseVerificationError("SHA256SUMS filenames are unexpected")
    return rows


def _load_local(
    dist_dir: Path,
    version: str,
) -> tuple[dict[str, str], dict[str, int]]:
    expected = _expected_names(version)
    # pypa/gh-action-pypi-publish writes `<dist>.publish.attestation`
    # sidecars into the distribution directory after uploading. They are
    # not distributions and are not on the registry, so the post-publish
    # verification must ignore them; anything else unexpected still fails.
    paths = {
        path.name: path
        for path in dist_dir.iterdir()
        if path.is_file() and not path.name.endswith(ATTESTATION_SUFFIX)
    }
    if set(paths) != expected:
        raise ReleaseVerificationError(
            "local distribution filenames are unexpected"
        )
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for filename, path in paths.items():
        content = path.read_bytes()
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ReleaseVerificationError(
                f"local artifact is too large: {filename}"
            )
        hashes[filename] = _digest(content)
        sizes[filename] = len(content)
    return hashes, sizes


def _fetch_release(registry: Registry, version: str) -> dict[str, Any] | None:
    url = f"{registry.api_base}/{PROJECT}/{version}/json"
    try:
        with urlopen(url, timeout=15) as response:
            document = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseVerificationError(
            f"registry API returned HTTP {exc.code}"
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(
            "registry API could not be read"
        ) from exc
    if not isinstance(document, dict) or not isinstance(
        document.get("urls"), list
    ):
        raise ReleaseVerificationError("registry API response is invalid")
    return document


def _remote_files(release: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if release is None:
        return {}
    files: dict[str, dict[str, Any]] = {}
    for item in release["urls"]:
        if not isinstance(item, dict) or not isinstance(
            item.get("filename"), str
        ):
            raise ReleaseVerificationError(
                "registry artifact metadata is invalid"
            )
        filename = item["filename"]
        if filename in files:
            raise ReleaseVerificationError(
                "registry repeats an artifact filename"
            )
        files[filename] = item
    return files


def _validate_url(url: object, registry: Registry, *, label: str) -> str:
    if not isinstance(url, str):
        raise ReleaseVerificationError(f"{label} artifact URL is invalid")
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReleaseVerificationError(
            f"{label} artifact URL has an invalid port"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != registry.artifact_host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReleaseVerificationError(f"{label} artifact URL is not approved")
    return url


def _verify_remote(
    registry: Registry,
    remote: dict[str, dict[str, Any]],
    expected_hashes: dict[str, str],
    *,
    expected_sizes: dict[str, int] | None,
    require_complete: bool,
) -> bool:
    expected = set(expected_hashes)
    if not set(remote).issubset(expected):
        raise ReleaseVerificationError(
            "registry contains an unexpected artifact"
        )
    for filename, item in remote.items():
        if item.get("yanked") is not False:
            raise ReleaseVerificationError(
                f"registry artifact is yanked: {filename}"
            )
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReleaseVerificationError(
                f"registry size is invalid: {filename}"
            )
        if size > MAX_ARTIFACT_BYTES:
            raise ReleaseVerificationError(
                f"registry artifact is too large: {filename}"
            )
        if expected_sizes is not None and size != expected_sizes[filename]:
            raise ReleaseVerificationError(
                f"registry size mismatch: {filename}"
            )
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(digest, str) or not hmac.compare_digest(
            digest, expected_hashes[filename]
        ):
            raise ReleaseVerificationError(
                f"registry hash mismatch: {filename}"
            )
        _validate_url(item.get("url"), registry, label="registry")
    complete = set(remote) == expected
    if require_complete and not complete:
        raise ReleaseVerificationError("registry release is incomplete")
    return complete


def preflight(repository: str, version: str, dist_dir: Path) -> None:
    registry = REGISTRIES[repository]
    hashes, sizes = _load_local(dist_dir, version)
    remote = _remote_files(_fetch_release(registry, version))
    complete = _verify_remote(
        registry,
        remote,
        hashes,
        expected_sizes=sizes,
        require_complete=False,
    )
    state = (
        "complete"
        if complete
        else f"recoverable ({len(remote)}/2 files present)"
    )
    print(f"{repository} preflight: {state}")


def wait_until_complete(
    repository: str,
    version: str,
    dist_dir: Path,
    timeout_seconds: int,
) -> None:
    registry = REGISTRIES[repository]
    hashes, sizes = _load_local(dist_dir, version)
    deadline = time.monotonic() + timeout_seconds
    while True:
        remote = _remote_files(_fetch_release(registry, version))
        if _verify_remote(
            registry,
            remote,
            hashes,
            expected_sizes=sizes,
            require_complete=False,
        ):
            print(f"{repository} release is complete and verified")
            return
        if time.monotonic() >= deadline:
            raise ReleaseVerificationError(
                "registry release remained incomplete"
            )
        time.sleep(5)


def download(
    repository: str,
    version: str,
    manifest: Path,
    dist_dir: Path,
    minimum_age_hours: int,
) -> None:
    registry = REGISTRIES[repository]
    hashes = _load_manifest(manifest, version)
    remote = _remote_files(_fetch_release(registry, version))
    _verify_remote(
        registry,
        remote,
        hashes,
        expected_sizes=None,
        require_complete=True,
    )
    uploaded: list[datetime] = []
    downloaded: dict[str, bytes] = {}
    for filename, item in remote.items():
        timestamp = item.get("upload_time_iso_8601")
        if not isinstance(timestamp, str):
            raise ReleaseVerificationError(
                f"upload timestamp is invalid: {filename}"
            )
        try:
            parsed_timestamp = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ReleaseVerificationError(
                f"upload timestamp is invalid: {filename}"
            ) from exc
        if parsed_timestamp.tzinfo is None:
            raise ReleaseVerificationError(
                f"upload timestamp has no timezone: {filename}"
            )
        uploaded.append(parsed_timestamp)

        url = _validate_url(item.get("url"), registry, label="registry")
        try:
            with urlopen(url, timeout=30) as response:
                _validate_url(response.geturl(), registry, label="final")
                content = response.read(MAX_ARTIFACT_BYTES + 1)
        except OSError as exc:
            raise ReleaseVerificationError(
                f"artifact download failed: {filename}"
            ) from exc
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ReleaseVerificationError(
                f"downloaded artifact is too large: {filename}"
            )
        if len(content) != item["size"]:
            raise ReleaseVerificationError(
                f"downloaded size mismatch: {filename}"
            )
        if not hmac.compare_digest(_digest(content), hashes[filename]):
            raise ReleaseVerificationError(
                f"downloaded hash mismatch: {filename}"
            )
        downloaded[filename] = content

    age = datetime.now(timezone.utc) - max(uploaded)
    if age < timedelta(hours=minimum_age_hours):
        raise ReleaseVerificationError(
            f"registry cooling-off is {age}; "
            f"{minimum_age_hours} hours is required"
        )
    dist_dir.mkdir(parents=True, exist_ok=False)
    for filename, content in downloaded.items():
        (dist_dir / filename).write_bytes(content)
    print(
        f"downloaded verified {repository} artifacts after cooling-off: {age}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "wait"):
        command = commands.add_parser(name)
        command.add_argument("--repository", choices=REGISTRIES, required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--dist-dir", type=Path, required=True)
        if name == "wait":
            command.add_argument("--timeout-seconds", type=int, default=120)
    download_command = commands.add_parser("download")
    download_command.add_argument(
        "--repository", choices=REGISTRIES, required=True
    )
    download_command.add_argument("--version", required=True)
    download_command.add_argument("--manifest", type=Path, required=True)
    download_command.add_argument("--dist-dir", type=Path, required=True)
    download_command.add_argument("--minimum-age-hours", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            preflight(args.repository, args.version, args.dist_dir)
        elif args.command == "wait":
            if args.timeout_seconds < 1 or args.timeout_seconds > 600:
                raise ReleaseVerificationError(
                    "timeout must be between 1 and 600 seconds"
                )
            wait_until_complete(
                args.repository,
                args.version,
                args.dist_dir,
                args.timeout_seconds,
            )
        else:
            if args.minimum_age_hours < 0 or args.minimum_age_hours > 168:
                raise ReleaseVerificationError(
                    "minimum age must be between 0 and 168 hours"
                )
            download(
                args.repository,
                args.version,
                args.manifest,
                args.dist_dir,
                args.minimum_age_hours,
            )
    except (OSError, ReleaseVerificationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
