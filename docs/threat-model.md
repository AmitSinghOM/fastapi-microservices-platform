# Webhook Platform Threat Model

## Scope and security goals

This model covers the FastAPI control plane, delivery worker, PostgreSQL queue,
management JWTs, project API keys, endpoint signing secrets, persisted payloads,
and outbound receiver traffic. It targets tenant isolation, durable queue
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

1. Management clients cross the public API boundary using bearer JWTs.
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
| Cross-tenant object access | Disclosure or modification | Membership-scoped queries; opaque public IDs | Phase 7 role and isolation matrix |
| API/JWT theft | Unauthorized ingestion or administration | Digests, peppers, revocation, bounded JWT life | Rotation, expiry, scopes, audit |
| Forged webhook | Receiver accepts attacker data | HMAC over timestamp and exact bytes | Receiver SDK and rotation overlap |
| Replay or duplicate | Repeated business action | Stable event ID; documented at-least-once contract | Shared replay quotas and audit |
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

Phase 4 quotas/fairness, Phase 5 retry budgets, and Phase 7 fine-grained roles
remain required before broad multi-tenant production claims.

## Security invariants

- Event and initial delivery rows commit atomically.
- Outbound HTTP never occurs inside a database transaction.
- A finalization requires the current processing state and matching lease token.
- Plaintext API keys and signing secrets are returned only at creation/rotation.
- Secrets, authorization headers, complete payloads, and complete responses are
  never logged.
- Automatic delivery remains at least once; duplicates are never presented as
  impossible.
- Deployed workers use an explicit CONNECT proxy, ignore ambient proxy bypass
  variables, and cannot route directly to the outbound network.
- Security deny metrics and audit logs contain only fixed layer/reason values,
  never destination URLs, hosts, addresses, credentials, queries, or bodies.
## Operational requirements

Production deployments must use HTTPS, stable managed secrets, least-privilege
database roles, encrypted backups, a trusted ingress, and worker-only egress
controls that deny private, link-local, metadata, control-plane, database, and
cluster networks. Operators must alert on queue age, lease expiry, repeated
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