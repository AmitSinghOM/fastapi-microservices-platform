# Phase 3 Egress and SSRF Boundary

## Security contract

Staging and production accept only HTTPS webhook URLs whose effective port is
443. Credentials, fragments, localhost names, non-global literals, every
non-global DNS answer, IPv4-mapped private IPv6, and unresolved names are denied
at endpoint admission and immediately before every attempt. Redirects are never
followed.

Application validation is defense in depth, not the network boundary. A worker
must set `WORKER_EGRESS_PROXY_URL` to a credential-free internal HTTP CONNECT
proxy. HTTPX receives that value explicitly, uses `trust_env=False`, disables
redirects and keepalive, and therefore ignores `HTTP_PROXY`, `HTTPS_PROXY`, and
`NO_PROXY` while forcing a fresh CONNECT and proxy DNS decision per attempt.

## Compose boundary

The reference topology has three networks:

- `backend` is internal and carries API/worker/PostgreSQL traffic.
- `worker-proxy` is internal and contains workers plus the proxy listener.
- `outbound` is joined by the proxy, but never by a worker.

The proxy cannot resolve or connect to backend service names because it is not
on `backend`. Workers cannot route directly to Internet destinations because
both of their networks are internal. The proxy port is not published.

Canonical's `ubuntu/squid:6.6-24.04_beta` image is pinned to multi-platform
index digest
`sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029`.
The wrapper validates configuration at build time and runs Squid as its
unprivileged `proxy` user with all capabilities dropped, no privilege
escalation, a read-only root filesystem, and a restricted temporary filesystem.

Squid accepts only CONNECT from the fixed worker subnet and only to TCP 443. Its
independent destination ACL denies IPv4/IPv6 unspecified, loopback, private,
shared, link-local, metadata, documentation, benchmark, multicast, reserved,
translation, mapped, unique-local, and deployment service ranges. TLS is not
intercepted; HTTPX still verifies the receiver certificate and SNI hostname.

## Deny observability

Application admission, attempt, and proxy denials increment a process-local
counter keyed only by fixed layer and reason enums. Audit records contain only
those two bounded fields. They never accept or emit a URL, hostname, resolved
address, query, credential, header, payload, or response. Squid access logging
is disabled because its ordinary request records contain destination data.
Phase 6 will export these bounded counters through the platform metrics system.

## Reproducible validation

Fast tests in `app/tests/test_phase3_ssrf.py` cover malformed and alternate
address forms, mapped IPv6, mixed DNS answers, credentials, fragments,
nonstandard ports, proxy settings, redirect refusal, proxy-deny classification,
and secret-free low-cardinality audit events.

The GitHub container workflow runs the black-box override in
`tests/egress/docker-compose.test.yml`. The harness is hermetic: it contacts
no real host or resolver. Every network in the test topology, including the
proxy's `outbound` network, is Docker-internal. The controlled DNS responder
is authoritative for a fixed set of `.test` names and answers anything else
with NXDOMAIN instead of forwarding. It provides private, metadata,
CNAME-to-private, mixed public/private, split-view, and
public-first/private-second responses.

The "public" target is a local TLS receiver at `11.200.0.10` on the internal
`simulated-public` network. That address is globally routable space, so it
falls outside every production deny rule and the production Squid ACLs run
unchanged; the test override only points Squid at the controlled resolver.
Because the network is internal, those addresses are shadowed only inside the
Compose project and traffic cannot leave the Docker host. Each run creates a
throwaway CA and receiver certificate with `tests/egress/tls_fixture.py`
(OpenSSL CLI, one-day validity, CA key deleted after signing, nothing
committed). The probe trusts only that CA, and the receiver echoes the SNI it
observed.

The probe bypasses application URL validation and verifies all of the
following:

1. A direct worker socket to the simulated public receiver's numeric address
   fails; the probe has the production worker's network attachments.
2. Raw private, alternate, mapped, metadata, CNAME, mixed, and rebound CONNECT
   targets receive proxy `403` responses. An unresolvable name is refused
   with `403` or `503` and never opens a tunnel.
3. Non-443 CONNECT and non-CONNECT methods are denied.
4. The public 443 receiver completes end-to-end TLS through the tunnel, and
   the receiver reports the requested SNI.
5. A mismatched TLS hostname fails certificate verification.
6. The shipped worker client (`app.worker.create_http_client`), run with
   hostile `HTTPS_PROXY`, `NO_PROXY=*`, and `SSL_CERT_FILE` set to the harness
   CA, is denied for a private target and rejects the receiver certificate,
   proving it uses the pinned proxy and ignores ambient trust settings.
7. An injected destination secret does not appear in any container log.

`tests/egress/test_harness.py` checks the harness itself without Docker: the
resolver never forwards, the simulated address is public yet outside the
production deny list, every harness network is internal, the test Squid file
contains no access rules, and the TLS fixture verifies and reports SNI.

Run locally on a disposable Docker host:

```bash
export PHASE3_TLS_FIXTURE_DIR="$(mktemp -d)"
python tests/egress/tls_fixture.py "$PHASE3_TLS_FIXTURE_DIR"
docker compose -f docker-compose.yml -f tests/egress/docker-compose.test.yml \
  up --build --abort-on-container-exit --exit-code-from egress-probe egress-probe
docker compose -f docker-compose.yml -f tests/egress/docker-compose.test.yml \
  down --volumes --remove-orphans
```

## Operator requirements

Equivalent non-Compose deployments must preserve two independent controls:
application all-answer validation and a network-enforced resolver/egress policy.
Do not attach workers directly to an outbound network. Keep proxy listeners
private, allow only required ports, add platform-specific pod/service/control-
plane/database CIDRs to the deny policy, and retain end-to-end certificate
verification. A deployment without this independent boundary does not satisfy
the Phase 3 security contract.

Content based on the [Canonical Squid image](https://hub.docker.com/r/ubuntu/squid)
and [Ubuntu Squid configuration guidance](https://ubuntu.com/server/docs/how-to/web-services/install-a-squid-server/)
was rephrased for compliance with licensing restrictions.
