"""Normalize a source distribution so identical inputs produce identical bytes.

setuptools writes sdists with wall-clock member mtimes (sub-second PAX
floats), the builder's uid/gid/user/group names, and a wall-clock gzip
header timestamp; it does not honor ``SOURCE_DATE_EPOCH`` for the tarball.
This tool rewrites the archive with:

- member mtimes fixed to ``SOURCE_DATE_EPOCH`` (required in the environment),
- uid/gid 0 and empty uname/gname,
- modes reduced to 0644 (files) or 0755 (directories and executables),
- members sorted by name in the USTAR format (no PAX headers),
- gzip mtime 0, no embedded filename, and a fixed compression level.

Member names and file contents are never modified. The archive is only
replaced after the normalized output is proven to contain exactly the same
members with exactly the same bytes, and the tool is idempotent.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import stat
import sys
import tarfile
from pathlib import Path

GZIP_LEVEL = 9


class NormalizationError(ValueError):
    """Raised when the archive cannot be normalized safely."""


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None or not raw.isdigit():
        raise NormalizationError("SOURCE_DATE_EPOCH must be set to an integer")
    return int(raw)


def _content_map(data: bytes) -> dict[str, tuple[str, str]]:
    """Map member name -> (type, sha256 of content) for equality checks."""
    result: dict[str, tuple[str, str]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                extracted = tar.extractfile(member)
                assert extracted is not None
                digest = hashlib.sha256(extracted.read()).hexdigest()
            elif member.isdir():
                digest = ""
            else:
                raise NormalizationError(
                    f"unsupported member type in sdist: {member.name}"
                )
            if member.name in result:
                raise NormalizationError(f"duplicate member: {member.name}")
            result[member.name] = (
                "dir" if member.isdir() else "file",
                digest,
            )
    return result


def normalize_bytes(data: bytes, epoch: int) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
        members = source.getmembers()
        entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
        for member in members:
            if member.isfile():
                extracted = source.extractfile(member)
                assert extracted is not None
                payload: bytes | None = extracted.read()
            elif member.isdir():
                payload = None
            else:
                raise NormalizationError(
                    f"unsupported member type in sdist: {member.name}"
                )
            info = tarfile.TarInfo(member.name)
            info.type = member.type
            info.size = 0 if payload is None else len(payload)
            info.mtime = epoch
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            executable = bool(member.mode & stat.S_IXUSR)
            info.mode = 0o755 if member.isdir() or executable else 0o644
            entries.append((info, payload))

    entries.sort(key=lambda item: item[0].name)

    tar_buffer = io.BytesIO()
    with tarfile.open(
        fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT
    ) as target:
        for info, payload in entries:
            target.addfile(
                info, None if payload is None else io.BytesIO(payload)
            )

    gz_buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=gz_buffer,
        mtime=0,
        compresslevel=GZIP_LEVEL,
    ) as gz:
        gz.write(tar_buffer.getvalue())
    return gz_buffer.getvalue()


def normalize_file(path: Path, epoch: int) -> bool:
    """Normalize ``path`` in place. Returns True if the bytes changed."""
    original = path.read_bytes()
    normalized = normalize_bytes(original, epoch)
    if _content_map(original) != _content_map(normalized):
        raise NormalizationError("normalized archive content differs")
    if normalize_bytes(normalized, epoch) != normalized:
        raise NormalizationError("normalization is not idempotent")
    if normalized == original:
        return False
    temp_path = path.with_name(path.name + ".normalized")
    temp_path.write_bytes(normalized)
    os.replace(temp_path, path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sdist", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        epoch = _source_date_epoch()
        for path in args.sdist:
            if not path.name.endswith(".tar.gz"):
                raise NormalizationError(f"not a .tar.gz sdist: {path}")
            changed = normalize_file(path, epoch)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state = "normalized" if changed else "already normalized"
            print(f"{path.name}: {state} sha256={digest}")
    except (NormalizationError, tarfile.TarError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
