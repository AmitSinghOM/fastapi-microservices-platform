# Phase 3 black-box harness

Run on a disposable Docker host:

```bash
docker compose -f docker-compose.yml \
  -f tests/egress/docker-compose.test.yml \
  up --build --abort-on-container-exit --exit-code-from egress-probe \
  egress-probe
docker compose -f docker-compose.yml \
  -f tests/egress/docker-compose.test.yml down --volumes --remove-orphans
```

The override starts a controlled DNS responder. It is test-only and must not be
used as a deployment resolver.
