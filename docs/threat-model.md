# Webhook Platform Threat Model

## Scope and security goals

This model covers the FastAPI control plane, optional same-origin operational
portal, delivery worker, PostgreSQL queue, management JWTs, project API keys,
endpoint signing secrets, persisted payloads, and outbound receiver traffic. It targets tenant isolation, durable queue
integrity, credential confidentiality, authentic webhook delivery, and bounded
egress. It does not claim that application URL checks alone form a production
SSRF boundary.

## Assets

- Organization membership, projects, endpoint configuration, and audit context.
- JWT signing key, API-key pepper, endpoint signing key, and derived secrets.
- Event payloads, idempotency keys, delivery state, attempts, and responses.
- PostgreSQL availability and integrity.
- Worker network identity, outbound addresses, and receiver trust.

## Trust boundaries and actors

1. The browser portal and management clients cross the public API boundary
   using bearer JWTs; the portal keeps its JWT only in page memory.
2. Producers cross a separate ingestion boundary using project API keys.
3. API and workers cross the database boundary with shared schema access.
4. Workers cross DNS, proxy/firewall, Internet, and receiver boundaries.
5. CI, dependencies, images, and maintainers cross the supply-chain boundary.

Actors include legitimate owners/members/producers, compromised tenant
credentials, malicious tenants, hostile receivers/DNS, Internet attackers,
compromised dependencies or workers, and operators making mistakes.

## Assumptions

- TLS terminates at a trusted ingress and PostgreSQL transport is protected.
- Deployment secrets are random, stable, separately managed, and never logged.
- PostgreSQL backups, access control, patching, and host security are external
  operator responsibilities.
- Production workers have a network-enforced egress policy; without it the
  deployment does not satisfy the SSRF security boundary.
- Receivers implement signature timestamp checks and durable deduplication.

## Threats and current controls

| Threat | Impact | Current controls | Residual work |
| --- | --- | --- | --- |
| Cross-tenant object access | Disclosure or modification | Organization/project role matrix; tenant-hiding `404`; visible denial `403` | Extend the matrix with every future management route |
| API/JWT theft | Unauthorized ingestion or administration | Digests, peppers, revocation, bounded JWT life, scoped expiring API keys, overlap rotation, immutable audit | Accepted: a stolen bearer token stays valid until `exp` (no revocation list; the minted `jti` is not checked); deactivation is enforced per request. Add staged root/pepper rotation tooling |
| Targeted login lockout | A chosen account cannot log in during the lockout window | Per-account, database-backed failure budget; the lock rejects before credential evaluation so a locked account is not a password oracle | Accepted risk: an unauthenticated attacker who knows an address can re-lock it indefinitely (availability, not access). Deployments needing stronger login availability should front `/auth/login` with CAPTCHA/WAF controls |
| Portal XSS or browser credential exposure | Administrative takeover or leaked one-time secrets | Same-origin external assets; strict CSP without inline/third-party code; text-only rendering; no bearer browser storage; password/API-key field clearing | A compromised browser, extension, host, or same-origin service remains trusted; operators must use HTTPS and avoid session recording |
| Forged webhook | Receiver accepts attacker data | HMAC-SHA256 over timestamp and exact bytes (`legacy`) or over event ID, timestamp, and exact bytes (`standard`, per Standard Webhooks); versioned secrets with bounded verification overlap | Receiver SDK in Phase 8 |
| Signature scheme downgrade or mismatch | Receiver verification breaks (delivery outage) or loses event-ID binding (`legacy` does not bind `Webhook-Id` into the signature) | Scheme changes require the endpoint-manage role and append an immutable `endpoint.updated` audit event listing `changed_fields`; deliveries snapshot the scheme at acceptance so accepted and replayed work is never re-signed; both schemes remain HMAC-SHA256 over exact bytes; the one-time secret is returned only when switching to `standard` | Receivers that require ID binding should pin the expected scheme and alert on format changes; revisit when 4.0 changes the default for new endpoints (ADR 0002) |
| Replay or duplicate | Repeated business action | Stable event ID; documented at-least-once contract; shared quotas and audited replay | Receiver deduplication remains required |
| Idempotency race | Duplicate event/fan-out | Unique constraint and conflict handling | Continue PostgreSQL stress coverage |
| Lease theft/stale write | Duplicate or corrupt outcome | Skip-locked claims and token finalization | Heartbeats and slot-aware claims |
| SSRF/DNS rebinding | Internal service or metadata access | URL/all-answer DNS checks, no redirects, HTTPS:443 deployed; isolated CONNECT proxy independently resolves and denies special networks | Equivalent policy required outside Compose; recurring bypass corpus |
| Receiver resource abuse | Socket, connection, or retry exhaustion | Timeouts, response cap, bounded concurrency | Fairness, circuit breakers, retry budgets |
| Payload/response leakage | Customer-data exposure | No body/secret logging; response cap | Retention, encryption, access audit |
| Database exhaustion | API and queue outage | Bounded pools/batches; measured baseline | Admission limits and fair scheduling |
| Supply-chain compromise | Code execution or secret theft | Pinned Python dependencies; CI review/scans | Signed releases and provenance |

## Abuse cases

- A tenant fans one event out to excessive endpoints or repeatedly replays work.
- A receiver returns failures indefinitely to amplify retries.
- DNS initially resolves globally and later rebinds to an internal address.
- A stale worker finalizes after another worker reclaims an expired lease.
- A malicious member enumerates another organization's identifiers.
- CI or a dependency attempts to read release credentials.

Phase 4 quotas/fairness, Phase 5 retry budgets, and Phase 7 fine-grained
roles, credential lifecycle, retention, and deletion controls are required
before broad multi-tenant production claims.

## Security invariants

- Event and initial delivery rows commit atomically.
- Outbound HTTP never occurs inside a database transaction.
- A finalization requires the current processing state and matching lease token.
- Plaintext API keys and signing secrets are returned only at creation/rotation
  and on an endpoint's switch to the `standard` signature scheme (the standard
  serialization of the same derived digest, disclosed exactly once).
- An endpoint's signature scheme is captured on every delivery at acceptance;
  changing the scheme requires the endpoint-manage role, appends an immutable
  audit event, and never alters the signature of accepted or replayed work.
- The optional portal stores bearer tokens only in page memory and renders API
  output through text nodes under a same-origin, no-inline-content CSP.
- Secrets, authorization headers, complete payloads, and complete responses are
  never logged.
- Automatic delivery remains at least once; duplicates are never presented as
  impossible.
- Deployed workers use an explicit CONNECT proxy, ignore ambient proxy bypass
  variables, and cannot route directly to the outbound network.
- Security deny metrics and audit logs contain only fixed layer/reason values,
  never destination URLs, hosts, addresses, credentials, queries, or bodies.
- Administrative audit rows reject update/delete operations and survive tenant
  deletion without mutable foreign-key relationships.
- Retention clears payload/response content only after policy age and terminal
  delivery checks; deletion-pending tenants accept no new mutations or events.

## Operational requirements

Production deployments must use HTTPS, stable managed secrets, least-privilege
database roles, encrypted backups, a trusted ingress, and worker-only egress
controls that deny private, link-local, metadata, control-plane, database, and
cluster networks. The ingress must enforce a request body-size limit (the
application refuses oversized declared Content-Length values, but chunked
bodies without a declared length are the ingress's responsibility), and the
ASGI server behind a proxy must be configured to trust forwarded client
addresses (for example uvicorn's proxy-headers support); otherwise the
per-client limiter keys every request to the proxy address and degrades into
a single global budget for login and registration. Set
`REGISTRATION_ENABLED=false` once a deployment's operator accounts exist.
Operators must alert on queue age, lease expiry, repeated
authorization failures, egress denies, and unusual replay volume.

Incident response should revoke affected credentials, pause compromised
endpoints or workers, preserve append-only attempts and relevant audit context,
and avoid logging additional sensitive bodies during investigation.

## Validation and review triggers

Required security tests include tenant isolation, concurrent idempotency,
competing claims, expired-lease recovery, stale-token rejection, signature
vectors, URL bypass cases, response bounds, and migration checks. The Phase 3
container harness additionally checks alternate IP notation, mapped IPv6,
CNAME/private and mixed DNS answers, split-view and public-to-private rebinding,
redirect refusal, non-443 denial, direct-socket and `NO_PROXY` bypass attempts,
end-to-end TLS/SNI, hostname mismatch rejection, and secret-free logs.

Review this model for any change to authentication, authorization, secrets,
cryptography, event envelopes, persistence, retries, replay, workers, egress,
retention, dependencies, deployment topology, or tenant boundaries. Record
newly accepted risks in the relevant phase evidence rather than weakening an
invariant silently.

## Out of scope for the current baseline

The current baseline does not promise multi-region failover, exactly-once
delivery, payload-level end-to-end encryption, a hardened hosted control plane,
or safety without a production egress boundary. These limitations must remain
visible in deployment and release documentation.
