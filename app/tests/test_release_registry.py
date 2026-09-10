"""Release-registry local loader: the state of dist/ after publishing.

Regression for the sdk-v0.1.0 TestPyPI runs: pypa/gh-action-pypi-publish
writes ``*.publish.attestation`` sidecars into dist/ after uploading, and
the post-publish verification then refused its own directory with
"local distribution filenames are unexpected" — never reaching the
registry — on every attempt.
"""

from pathlib import Path

import pytest

from scripts.release_registry import (
    ATTESTATION_SUFFIX,
    ReleaseVerificationError,
    _load_local,
)

VERSION = "0.1.0"
WHEEL = f"fastapi_microservices_platform_sdk-{VERSION}-py3-none-any.whl"
SDIST = f"fastapi_microservices_platform_sdk-{VERSION}.tar.gz"


def _write_distributions(dist: Path) -> None:
    (dist / WHEEL).write_bytes(b"wheel-bytes")
    (dist / SDIST).write_bytes(b"sdist-bytes")


def test_loads_exact_distributions(tmp_path: Path) -> None:
    _write_distributions(tmp_path)
    hashes, sizes = _load_local(tmp_path, VERSION)
    assert set(hashes) == {WHEEL, SDIST}
    assert sizes[WHEEL] == len(b"wheel-bytes")


def test_ignores_publish_attestation_sidecars(tmp_path: Path) -> None:
    """The exact post-publish directory state that broke verification."""
    _write_distributions(tmp_path)
    (tmp_path / (WHEEL + ATTESTATION_SUFFIX)).write_text("{}")
    (tmp_path / (SDIST + ATTESTATION_SUFFIX)).write_text("{}")
    hashes, _ = _load_local(tmp_path, VERSION)
    assert set(hashes) == {WHEEL, SDIST}


def test_still_rejects_other_unexpected_files(tmp_path: Path) -> None:
    _write_distributions(tmp_path)
    (tmp_path / "stray.txt").write_text("nope")
    with pytest.raises(ReleaseVerificationError, match="unexpected"):
        _load_local(tmp_path, VERSION)


def test_rejects_missing_distribution(tmp_path: Path) -> None:
    (tmp_path / WHEEL).write_bytes(b"wheel-bytes")
    with pytest.raises(ReleaseVerificationError, match="unexpected"):
        _load_local(tmp_path, VERSION)
