import pytest

from scripts.check_deployment_lock import check, read_pins

HASH = "--hash=sha256:" + "a" * 64


def test_extras_and_canonical_names_match():
    assert check("Example_Pkg[extra]==1.2\n", f"example-pkg==1.2 {HASH}") == 1


@pytest.mark.parametrize("locked", ["other==1.2", "example==1.3"])
def test_direct_pin_drift_is_rejected(locked):
    with pytest.raises(ValueError, match="drift"):
        check("example==1.2", f"{locked} {HASH}")


@pytest.mark.parametrize(
    "lock",
    [
        "",
        "example>=1",
        "example==1",
        "example==1 --hash=md5:abc",
        "example==1 --hash=sha256:bad",
        "-r other.txt",
    ],
)
def test_incomplete_or_untrusted_lock_format_is_rejected(lock):
    with pytest.raises(ValueError):
        read_pins(lock, hashed=True)


def test_duplicate_normalized_name_is_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        read_pins("some_pkg==1\nsome-pkg==1", hashed=False)


def test_continuation_hashes_and_transitive_pins_are_supported():
    lock = f"example==1 \\\n    {HASH}\nother==2 {HASH} # transitive\n"
    assert check("example==1 # direct\n", lock) == 2
