"""Tests for scripts/normalize_sdist.py."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import struct
import sys
import tarfile
import time
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "normalize_sdist.py"
)
spec = importlib.util.spec_from_file_location("normalize_sdist", MODULE_PATH)
assert spec is not None and spec.loader is not None
normalize_sdist = importlib.util.module_from_spec(spec)
sys.modules["normalize_sdist"] = normalize_sdist
spec.loader.exec_module(normalize_sdist)

EPOCH = 1_700_000_000


def _build_sdist(
    *,
    names_in_order: list[str],
    mtime: float,
    uid: int,
    uname: str,
    executable: str | None = None,
    gzip_mtime: float | None = None,
) -> bytes:
    """Create a setuptools-like sdist: PAX floats, builder identity, wall gzip."""
    tar_buffer = io.BytesIO()
    with tarfile.open(
        fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT
    ) as tar:
        root = tarfile.TarInfo("pkg-1.0")
        root.type = tarfile.DIRTYPE
        root.mode = 0o775
        root.mtime = mtime
        root.uid, root.gid, root.uname, root.gname = uid, 20, uname, "staff"
        tar.addfile(root)
        for name in names_in_order:
            payload = f"content of {name}\n".encode()
            info = tarfile.TarInfo(f"pkg-1.0/{name}")
            info.size = len(payload)
            info.mtime = mtime + names_in_order.index(name) * 0.013
            info.uid, info.gid, info.uname, info.gname = (
                uid,
                20,
                uname,
                "staff",
            )
            info.mode = 0o755 if name == executable else 0o664
            tar.addfile(info, io.BytesIO(payload))
    gz_buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="pkg-1.0.tar",
        mode="wb",
        fileobj=gz_buffer,
        mtime=time.time() if gzip_mtime is None else gzip_mtime,
    ) as gz:
        gz.write(tar_buffer.getvalue())
    return gz_buffer.getvalue()


def test_two_differing_builds_normalize_to_identical_bytes() -> None:
    first = _build_sdist(
        names_in_order=["b.py", "a.py", "PKG-INFO"],
        mtime=1_789_154_867.36,
        uid=503,
        uname="amisinc",
    )
    second = _build_sdist(
        names_in_order=["PKG-INFO", "a.py", "b.py"],
        mtime=1_789_154_874.27,
        uid=1001,
        uname="runner",
    )
    assert first != second
    assert normalize_sdist.normalize_bytes(
        first, EPOCH
    ) == normalize_sdist.normalize_bytes(second, EPOCH)


def test_normalization_preserves_names_and_contents() -> None:
    original = _build_sdist(
        names_in_order=["z.py", "y.py"],
        mtime=1_789_154_867.36,
        uid=503,
        uname="amisinc",
    )
    normalized = normalize_sdist.normalize_bytes(original, EPOCH)
    assert normalize_sdist._content_map(
        original
    ) == normalize_sdist._content_map(normalized)


def test_normalized_metadata_is_fixed() -> None:
    original = _build_sdist(
        names_in_order=["run.sh", "lib.py"],
        mtime=1_789_154_867.36,
        uid=503,
        uname="amisinc",
        executable="run.sh",
    )
    normalized = normalize_sdist.normalize_bytes(original, EPOCH)
    assert struct.unpack("<I", normalized[4:8])[0] == 0  # gzip mtime
    with tarfile.open(fileobj=io.BytesIO(normalized), mode="r:gz") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == sorted(m.name for m in members)
    assert {m.mtime for m in members} == {EPOCH}
    assert {(m.uid, m.gid, m.uname, m.gname) for m in members} == {
        (0, 0, "", "")
    }
    modes = {m.name.rsplit("/", 1)[-1]: m.mode for m in members}
    assert modes["run.sh"] == 0o755
    assert modes["lib.py"] == 0o644
    assert modes["pkg-1.0"] == 0o755
    for member in members:
        assert member.pax_headers == {}


def test_normalization_is_idempotent() -> None:
    original = _build_sdist(
        names_in_order=["a.py"],
        mtime=1_789_154_867.36,
        uid=503,
        uname="amisinc",
    )
    once = normalize_sdist.normalize_bytes(original, EPOCH)
    assert normalize_sdist.normalize_bytes(once, EPOCH) == once


def test_normalize_file_rewrites_and_reports_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    path = tmp_path / "pkg-1.0.tar.gz"
    path.write_bytes(
        _build_sdist(
            names_in_order=["a.py"],
            mtime=1_789_154_867.36,
            uid=503,
            uname="amisinc",
        )
    )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert normalize_sdist.main([str(path)]) == 0
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before != after
    assert normalize_sdist.normalize_file(path, EPOCH) is False
    assert not list(tmp_path.glob("*.normalized"))


def test_missing_source_date_epoch_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    path = tmp_path / "pkg-1.0.tar.gz"
    path.write_bytes(
        _build_sdist(names_in_order=["a.py"], mtime=1.0, uid=0, uname="")
    )
    assert normalize_sdist.main([str(path)]) == 2
    assert "SOURCE_DATE_EPOCH" in capsys.readouterr().err


def test_symlink_members_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
        link = tarfile.TarInfo("pkg-1.0/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    path = tmp_path / "pkg-1.0.tar.gz"
    path.write_bytes(gzip.compress(tar_buffer.getvalue()))
    assert normalize_sdist.main([str(path)]) == 2


def test_non_tarball_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    path = tmp_path / "pkg-1.0-py3-none-any.whl"
    path.write_bytes(b"not a tarball")
    assert normalize_sdist.main([str(path)]) == 2
