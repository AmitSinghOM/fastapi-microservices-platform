"""Create a throwaway CA and receiver certificate for the egress harness.

Run on the Docker host before starting the harness:

    python tests/egress/tls_fixture.py "$PHASE3_TLS_FIXTURE_DIR"

The CA private key is deleted after signing, so the CA can never issue a
second certificate. Nothing here is committed; each run gets fresh keys
that are valid for one day and trusted only by the harness probe.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RECEIVER_NAME = "receiver.public.test"
CA_CERTIFICATE = "ca.pem"
RECEIVER_CERTIFICATE = "receiver.pem"
RECEIVER_KEY = "receiver-key.pem"

CA_EXTENSIONS = """\
[req]
distinguished_name = subject
x509_extensions = ca
prompt = no
[subject]
CN = Phase 3 egress harness CA
[ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""

RECEIVER_EXTENSIONS = f"""\
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:{RECEIVER_NAME}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""

# RSA keeps OpenSSL and LibreSSL output identical (EC defaults differ).
NEW_KEY = ["-newkey", "rsa:2048"]


def openssl(*arguments: str) -> None:
    executable = shutil.which("openssl")
    if executable is None:
        raise SystemExit("openssl is required to create the TLS fixture")
    subprocess.run(
        [executable, *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def create_fixture(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch_name:
        scratch = Path(scratch_name)
        (scratch / "ca.cnf").write_text(CA_EXTENSIONS)
        (scratch / "receiver.ext").write_text(RECEIVER_EXTENSIONS)
        ca_key = scratch / "ca-key.pem"
        request = scratch / "receiver.csr"
        openssl(
            "req", "-x509", *NEW_KEY, "-nodes", "-days", "1",
            "-config", str(scratch / "ca.cnf"),
            "-keyout", str(ca_key),
            "-out", str(output / CA_CERTIFICATE),
        )  # fmt: skip
        openssl(
            "req", "-new", *NEW_KEY, "-nodes",
            "-subj", f"/CN={RECEIVER_NAME}",
            "-keyout", str(output / RECEIVER_KEY),
            "-out", str(request),
        )  # fmt: skip
        openssl(
            "x509", "-req", "-in", str(request), "-days", "1",
            "-CA", str(output / CA_CERTIFICATE), "-CAkey", str(ca_key),
            "-set_serial", str(secrets.randbits(63)),
            "-extfile", str(scratch / "receiver.ext"),
            "-out", str(output / RECEIVER_CERTIFICATE),
        )  # fmt: skip
    # Containers run as a different uid than the host user. The key is an
    # ephemeral test credential trusted by nothing outside this harness.
    output.chmod(0o755)
    for name in (CA_CERTIFICATE, RECEIVER_CERTIFICATE, RECEIVER_KEY):
        (output / name).chmod(0o644)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: tls_fixture.py OUTPUT_DIRECTORY")
    create_fixture(Path(sys.argv[1]))
