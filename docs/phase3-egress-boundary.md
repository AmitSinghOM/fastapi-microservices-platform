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
`tests/egress/docker-compose.test.yml`. Its controlled DNS responder provides
private, metadata, CNAME-to-private, mixed public/private, split-view, and
public-first/private-second responses. The probe bypasses application URL
validation and verifies all of the following:

1. A direct worker socket cannot reach a public TLS endpoint.
2. Raw private, alternate, mapped, metadata, CNAME, mixed, and rebound CONNECT
   targets receive proxy `403` responses.
3. Non-443 CONNECT and non-CONNECT methods are denied.
4. A valid public 443 target completes end-to-end TLS with correct SNI.
5. A mismatched TLS hostname fails certificate verification.
6. Ambient proxy variables and `NO_PROXY=*` do not create a bypass.
7. An injected destination secret does not appear in any container log.

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
