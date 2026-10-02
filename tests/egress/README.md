# Phase 3 black-box harness

Hermetic: every network is Docker-internal, the controlled DNS responder never
forwards, and the "public" receiver is a local TLS server on a simulated
public subnet. See `docs/phase3-egress-boundary.md` for what it proves.

Run on a disposable Docker host:

```bash
export PHASE3_TLS_FIXTURE_DIR="$(mktemp -d)"
python tests/egress/tls_fixture.py "$PHASE3_TLS_FIXTURE_DIR"
docker compose -f docker-compose.yml \
  -f tests/egress/docker-compose.test.yml \
  up --build --abort-on-container-exit --exit-code-from egress-probe \
  egress-probe
docker compose -f docker-compose.yml \
  -f tests/egress/docker-compose.test.yml down --volumes --remove-orphans
```

Docker-free checks of the harness itself: `pytest tests/egress -q`.

The DNS responder, receiver, and fixture are test-only and must never be used
in a deployment.
