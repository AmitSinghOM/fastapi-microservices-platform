"""Phase 8 scripted clean-machine gate (amended completion gate, ADR-adjacent).

Starts from a fresh clone and a fresh virtual environment, then follows
``docs/phase8-adoption.md`` verbatim: install the SDK, authenticate with
``webhookctl``, create an organization, project, producer key, and endpoint,
send an event with a stable idempotency key, verify the exact-body signature
in the example receiver (durably accepting the event ID once), and inspect
the delivery through the CLI. Every step is timed; the run fails on any
broken step or when the total exceeds the 30-minute budget.

The run uses ``ENVIRONMENT=development`` with ``ALLOW_PRIVATE_WEBHOOKS=true``
so the local example receiver is a permitted target; this flag is refused
outside development at configuration load. SQLite backs both runtimes, which
matches the documented local quick start (PostgreSQL semantics are covered
by the opt-in concurrency suite, not this gate).

Usage:
    python scripts/phase8_clean_machine_gate.py [--budget-seconds 1800]
        [--repo PATH] [--report FILE] [--keep]

Exit status 0 means the gate passed. The JSON report contains only step
names, durations, and bounded error text — never secrets or payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PASSWORD = "clean-machine-gate-password"
EVENT_TYPE = "gate.order.created"
IDEMPOTENCY_KEY = "gate-order-0001"


class GateFailure(Exception):
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Gate:
    def __init__(self, repo: Path, budget_seconds: int, keep: bool):
        self.repo = repo
        self.budget = budget_seconds
        self.keep = keep
        self.started = time.monotonic()
        self.steps: list[dict] = []
        self.workspace = Path(tempfile.mkdtemp(prefix="phase8-gate-"))
        self.clone = self.workspace / "repo"
        self.venv = self.workspace / "venv"
        self.bin = self.venv / "bin"
        self.api_port = free_port()
        self.receiver_port = free_port()
        self.receiver_db = self.workspace / "receiver-inbox.db"
        self.processes: list[subprocess.Popen] = []
        self.base_env = {
            **os.environ,
            "ENVIRONMENT": "development",
            "ALLOW_PRIVATE_WEBHOOKS": "true",
            "SECRET_KEY": "g" * 40,
            "API_KEY_PEPPER": "p" * 40,
            "WEBHOOK_SIGNING_KEY": "w" * 40,
            "DATABASE_URL": (
                f"sqlite+aiosqlite:///{self.workspace}/platform.db"
            ),
            "OBSERVABILITY_ENABLED": "false",
            "WEBHOOK_PLATFORM_URL": f"http://127.0.0.1:{self.api_port}",
            "WEBHOOK_PLATFORM_CREDENTIALS": (
                f"{self.workspace}/credentials.json"
            ),
        }

    # -- bookkeeping ------------------------------------------------------

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def step(self, name: str):
        gate = self

        class _Step:
            def __enter__(self):
                self.begun = time.monotonic()
                print(f"[{gate.elapsed():7.1f}s] {name} ...", flush=True)
                return self

            def __exit__(self, exc_type, exc, tb):
                duration = time.monotonic() - self.begun
                record = {
                    "step": name,
                    "seconds": round(duration, 2),
                    "ok": exc_type is None,
                }
                if exc is not None:
                    record["error"] = str(exc)[:300]
                gate.steps.append(record)
                if gate.elapsed() > gate.budget and exc_type is None:
                    raise GateFailure(
                        f"budget exceeded after step '{name}' "
                        f"({gate.elapsed():.0f}s > {gate.budget}s)"
                    )
                return False

        return _Step()

    def run(
        self,
        *command: str,
        env: dict | None = None,
        cwd: Path | None = None,
        stdin_text: str | None = None,
    ) -> str:
        result = subprocess.run(
            command,
            cwd=cwd or self.clone,
            env=env or self.base_env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise GateFailure(
                f"{command[0]} exited {result.returncode}: "
                f"{(result.stderr or result.stdout)[-300:]}"
            )
        return result.stdout

    def ctl(self, *args: str, stdin_text: str | None = None):
        output = self.run(
            str(self.bin / "webhookctl"), *args, stdin_text=stdin_text
        )
        return json.loads(output)

    def spawn(
        self, *command: str, env: dict | None = None, cwd: Path | None = None
    ) -> subprocess.Popen:
        process = subprocess.Popen(
            command,
            cwd=cwd or self.clone,
            env=env or self.base_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        return process

    def wait_http(self, url: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status < 500:
                        return
            except Exception:
                time.sleep(0.5)
        raise GateFailure(f"service at {url} not ready within {timeout}s")

    def cleanup(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in reversed(self.processes):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        if not self.keep:
            shutil.rmtree(self.workspace, ignore_errors=True)

    # -- the gate ---------------------------------------------------------

    def execute(self) -> None:
        with self.step("clone repository into clean workspace"):
            subprocess.run(
                ["git", "clone", "--quiet", str(self.repo), str(self.clone)],
                check=True,
                capture_output=True,
            )

        with self.step("create clean virtual environment"):
            subprocess.run(
                [sys.executable, "-m", "venv", str(self.venv)],
                check=True,
                capture_output=True,
            )

        with self.step("pip install platform requirements"):
            self.run(
                str(self.bin / "pip"),
                "install",
                "--quiet",
                "-r",
                "requirements.txt",
            )

        with self.step("pip install SDK and receiver example"):
            self.run(
                str(self.bin / "pip"), "install", "--quiet", "./sdk/python"
            )
            # The receiver README says "from this directory": its
            # requirements file uses a relative editable SDK path.
            self.run(
                str(self.bin / "pip"),
                "install",
                "--quiet",
                "-r",
                "requirements.txt",
                cwd=self.clone / "examples" / "receiver",
            )

        with self.step("start API and wait for readiness"):
            self.spawn(
                str(self.bin / "python"),
                "-m",
                "uvicorn",
                "app.main:app",
                "--port",
                str(self.api_port),
                "--host",
                "127.0.0.1",
            )
            self.wait_http(
                f"http://127.0.0.1:{self.api_port}/readyz", timeout=90
            )

        with self.step("register and log in with webhookctl"):
            self.ctl(
                "auth",
                "register",
                "--email",
                "gate@example.com",
                "--name",
                "Gate Runner",
                stdin_text=f"{PASSWORD}\n{PASSWORD}\n",
            )
            self.ctl(
                "auth",
                "login",
                "--email",
                "gate@example.com",
                stdin_text=f"{PASSWORD}\n",
            )

        with self.step("create organization, project, producer key"):
            organization = self.ctl(
                "organizations", "create", "--name", "Gate Organization"
            )
            project = self.ctl(
                "projects",
                "create",
                "--organization",
                organization["public_id"],
                "--name",
                "Gate Project",
            )
            self.project_id = project["public_id"]
            key = self.ctl(
                "api-keys",
                "create",
                "--project",
                self.project_id,
                "--name",
                "gate-producer",
            )
            self.api_key = key["plaintext_key"]

        with self.step("create endpoint targeting the local receiver"):
            endpoint = self.ctl(
                "endpoints",
                "create",
                "--project",
                self.project_id,
                "--url",
                f"http://127.0.0.1:{self.receiver_port}/webhooks",
                "--signature-scheme",
                "standard",
            )
            self.signing_secret = endpoint["signing_secret"]

        with self.step("start example receiver with the one-time secret"):
            self.spawn(
                str(self.bin / "python"),
                "-m",
                "uvicorn",
                "app:app",
                "--port",
                str(self.receiver_port),
                "--host",
                "127.0.0.1",
                cwd=self.clone / "examples" / "receiver",
                env={
                    **self.base_env,
                    "WEBHOOK_SIGNING_SECRET": self.signing_secret,
                    "RECEIVER_DATABASE": str(self.receiver_db),
                },
            )
            self.wait_http(
                f"http://127.0.0.1:{self.receiver_port}/docs", timeout=60
            )

        with self.step("send event with a stable idempotency key"):
            payload = self.workspace / "payload.json"
            payload.write_text('{"order_id": "gate-1", "total": 42}')
            self.run(
                str(self.bin / "webhookctl"),
                "events",
                "send",
                "--type",
                EVENT_TYPE,
                "--idempotency-key",
                IDEMPOTENCY_KEY,
                "--payload-file",
                str(payload),
                env={
                    **self.base_env,
                    "WEBHOOK_PLATFORM_API_KEY": self.api_key,
                },
            )

        with self.step("run worker until the delivery succeeds"):
            worker = self.spawn(str(self.bin / "python"), "-m", "app.worker")
            deadline = time.monotonic() + 120
            delivery = None
            while time.monotonic() < deadline:
                rows = self.ctl(
                    "deliveries", "list", "--project", self.project_id
                )
                if rows and rows[0]["status"] == "succeeded":
                    delivery = rows[0]
                    break
                if rows and rows[0]["status"] == "dead":
                    raise GateFailure("delivery finished dead")
                time.sleep(2)
            if delivery is None:
                raise GateFailure("delivery did not succeed within 120s")
            self.delivery_id = delivery["public_id"]
            worker.send_signal(signal.SIGTERM)
            worker.wait(timeout=30)

        with self.step("verify durable signed acceptance in the receiver"):
            with sqlite3.connect(self.receiver_db) as connection:
                rows = connection.execute(
                    "SELECT event_id, event_type FROM inbox"
                ).fetchall()
            if len(rows) != 1 or rows[0][1] != EVENT_TYPE:
                raise GateFailure(
                    f"receiver inbox has {len(rows)} rows; expected exactly "
                    "one verified acceptance"
                )

        with self.step("inspect the delivery and its attempts via the CLI"):
            detail = self.ctl(
                "deliveries",
                "get",
                "--project",
                self.project_id,
                "--delivery",
                self.delivery_id,
            )
            if detail.get("status") != "succeeded":
                raise GateFailure("delivery detail is not 'succeeded'")
            attempts = self.ctl(
                "deliveries",
                "attempts",
                "--project",
                self.project_id,
                "--delivery",
                self.delivery_id,
            )
            if not attempts:
                raise GateFailure("delivery has no recorded attempts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_repo = Path(__file__).resolve().parent.parent
    parser.add_argument("--repo", type=Path, default=default_repo)
    parser.add_argument("--budget-seconds", type=int, default=1_800)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    gate = Gate(args.repo, args.budget_seconds, args.keep)
    passed = False
    error = None
    try:
        gate.execute()
        passed = gate.elapsed() <= args.budget_seconds
        if not passed:
            error = (
                f"total {gate.elapsed():.0f}s exceeded the "
                f"{args.budget_seconds}s budget"
            )
    except (
        GateFailure,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        error = str(exc)[:300]
    finally:
        gate.cleanup()

    report = {
        "gate": "phase8-clean-machine",
        "passed": passed,
        "total_seconds": round(gate.elapsed(), 1),
        "budget_seconds": args.budget_seconds,
        "steps": gate.steps,
        "error": error,
    }
    output = json.dumps(report, indent=2)
    print(output)
    if args.report:
        args.report.write_text(output)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
