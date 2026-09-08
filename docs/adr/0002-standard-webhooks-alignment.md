# ADR 0002: Standard Webhooks alignment as a versioned wire change

- Status: Proposed
- Date: 2026-09-08
- Decision owner: repository owner
- Reference: [Standard Webhooks specification](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)

## Context

The platform's signature scheme is nearly, but not exactly, the
[Standard Webhooks](https://www.standardwebhooks.com/) specification.
Aligning would let every receiver use any off-the-shelf Standard Webhooks
verification library in any language, which removes the largest remaining
adoption cost of this platform: today receivers must implement our bespoke
verification rules from [the wire protocol](../wire-protocol.md).

The divergences, verified against the current implementation
(`app/webhook_security.py`, `app/services/delivery_service.py`,
`sdk/python/src/webhook_platform_sdk/receiver.py`) and the specification:

| Aspect | Current (`legacy`) | Standard Webhooks |
| --- | --- | --- |
| Signature header value | `t=<seconds>,v1=<lowercase-hex>` | Space-delimited list of `v1,<base64>` |
| Signed content | `timestamp || "." || body` | `msg_id || "." || timestamp || "." || body` |
| Signature encoding | Lowercase hex | Standard base64 |
| HMAC key material | ASCII bytes of the full `whsec_…` string | Decoded bytes of the base64 after `whsec_` |
| Secret serialization | `whsec_` + base64url, unpadded | `whsec_` + standard base64 |
| ID/timestamp headers | `Webhook-Id`, `Webhook-Timestamp` | `webhook-id`, `webhook-timestamp` (identical; HTTP header names are case-insensitive) |
| Extra headers | `Webhook-Event`, `Webhook-Attempt` | Not specified; additive extras are permitted |

Two facts make this a **wire-breaking** change rather than the additive
migration the specification suggests:

1. The specification's migration advice — "add the Standard Webhooks headers
   in addition to any existing headers" — does not apply cleanly here,
   because our legacy header **is** `webhook-signature` (case-insensitively)
   with an incompatible value syntax. There is no free second header slot.
2. The signed content and key material differ, so the schemes cannot share
   one signature value even if the encoding matched.

Meanwhile the parts that already align must be preserved: `Webhook-Id` is
stable across attempts and replay (the spec's idempotency contract),
`Webhook-Timestamp` is integer unix seconds, our derived 32-byte secrets fall
inside the spec's 24–64-byte requirement, and the documented receiver rule
"parse `t` and one or more `v1` values" tolerates unknown space-delimited
tokens. Retry statuses, HTTPS enforcement, the egress proxy (the spec's own
SSRF recommendation), fan-out, replay, and per-endpoint event-type filtering
already satisfy the spec's operational sections.

## Decision

Introduce a per-endpoint, delivery-snapshotted **signature scheme** with two
values, and stage defaults across major versions. The envelope body, event
identity, retries, and all other wire behavior are unchanged.

A mixed-syntax "dual" header (`t=<T>,v1=<hex> v1,<base64>`) was considered
and rejected during review: the shipped SDK receiver's parser splits the
whole header on commas, so every existing receiver built on it rejects such
a header outright. A bridge that breaks the reference implementation is not
a bridge. Migration is instead **receiver-first** (below), which achieves
zero downtime without mixed headers.

### Scheme definitions

- **`legacy`** — today's exact bytes: header value `t=<T>,v1=<hex>`, signed
  content `T.body`, HMAC key = ASCII of the full `whsec_…` string. Remains
  byte-for-byte frozen and regression-locked.
- **`standard`** — spec-exact emission:
  - `webhook-signature: v1,<base64>` where the signed content is
    `event_id.timestamp.body` and the HMAC key is the **raw 32-byte derived
    digest** for the endpoint's secret version. A single-element list is
    spec-valid.
  - Secret rotation keeps the existing snapshot-version model: a delivery
    signs with the secret version captured at acceptance, and receivers may
    test the bounded active and previous secrets during an overlap window,
    exactly as today. Emitting multiple space-delimited signatures is
    therefore unnecessary and is not part of this design.
  - `Webhook-Event` and `Webhook-Attempt` continue to be sent; the spec
    permits additive headers.

### Receiver-first migration (replaces "dual")

1. The SDK receiver gains scheme auto-detection: header tokens shaped
   `t=…`/`v1=<hex>` verify as `legacy`; tokens shaped `v1,<base64>` verify
   as `standard`; unrecognized tokens are ignored rather than treated as
   malformed. Either secret serialization is accepted and normalized.
2. A receiver deploys the updated SDK (or any Standard Webhooks library
   plus the standard-form secret) while its endpoint still uses `legacy`.
3. The endpoint owner flips the endpoint to `standard`. In-flight and
   replayed deliveries continue to verify because they carry their
   acceptance-time scheme snapshot.

### Secret serialization per scheme

Both serializations encode the same derived 32-byte digest
`D = HMAC-SHA256(signing_key, "endpoint:<public-id>:v<version>")`, but they
are distinct key material and are presented distinctly:

- `legacy` secret string: `whsec_` + base64url(D), unpadded. HMAC key =
  ASCII bytes of that entire string (today's behavior, unchanged).
- `standard` secret string: `whsec_` + standard-base64(D), padded. HMAC key
  = D itself, so pasting the string into any Standard Webhooks library
  works without configuration.

Because both forms encode the same digest, the standard form is derivable
from the legacy form; returning it at scheme switch discloses nothing the
endpoint owner does not already hold. Disclosure surfaces are exact:

- `POST /v1/projects/{id}/endpoints` returns the one-time secret in the
  serialization matching the requested scheme.
- `PATCH …/endpoints/{id}` changing `signature_scheme` to `standard`
  returns a one-time `signing_secret` field (standard form) in the update
  response; all other updates omit the field.
- Secret-version rotation responses use the endpoint's current scheme's
  serialization.

### Snapshot and immutability rules

`deliveries` gains a `signature_scheme` snapshot captured at acceptance,
exactly like the existing URL and secret-version snapshots. Accepted
deliveries sign per their snapshot regardless of later endpoint edits;
replay copies the original snapshot. Changing an endpoint's scheme affects
only future acceptances. This preserves the platform's core invariant that
accepted work is immutable.

### Schema and API changes

- Migration `0011`: `webhook_endpoints.signature_scheme` (`legacy|standard`,
  NOT NULL, server default `legacy`) with a CHECK constraint, and
  `deliveries.signature_scheme` backfilled to `legacy`. Downgrade refuses
  while any non-legacy row exists, mirroring the `0008` envelope-mode rule.
- `POST /v1/projects/{id}/endpoints` accepts optional
  `signature_scheme` (default `legacy` in 3.x); `PATCH` allows changing it
  per the disclosure rules above.
- SDK: `create_endpoint`/`update_endpoint` pass-through; the receiver module
  gains auto-detected spec verification (`v1,<base64>` over
  `id.timestamp.body`) alongside legacy; `webhookctl` gains
  `--signature-scheme`.
- The portal and organization export include the scheme as a plain field.

### Version staging

| Release | Behavior |
| --- | --- |
| 3.x (next minor) | Feature ships complete but inert: everything defaults to `legacy`; `standard` is per-endpoint opt-in. Wire bytes of existing endpoints are untouched, so this is a minor release. |
| 4.0 | New endpoints default to `standard`. Existing endpoints keep their stored scheme; nothing migrates automatically. README and portal recommend `standard`. |
| 5.0 (earliest) | `legacy` may be refused for newly created endpoints. Existing legacy endpoints continue to work indefinitely; in-flight and replayed deliveries always honor their snapshots. |

This mirrors the deprecation contract already used for the tutorial items
API (default-off in 4.0, removal not before 5.0) in
[the release policy](../release-policy.md).

### Acceptance gates (evidence required before marking implemented)

1. Golden test vectors for both schemes added to
   [the wire protocol](../wire-protocol.md).
2. CI cross-verification: `standard`-scheme deliveries verified by the
   official `standard-webhooks` Python library (test-only dependency), not
   only by our own SDK.
3. Byte-for-byte regression lock on `legacy` emission (existing signature
   tests must pass unmodified).
4. SDK receiver auto-detects and verifies both schemes, accepts either
   secret serialization, and ignores unrecognized signature tokens instead
   of failing closed on them (the prerequisite for receiver-first
   migration).
5. Threat-model addendum: scheme downgrade requires endpoint-manage
   permission and is audited (`endpoint.updated` with `changed_fields`).

## Consequences

- Receivers gain drop-in compatibility with the Standard Webhooks library
  ecosystem (Python, JS, Go, Java, Ruby, PHP, Rust), eliminating bespoke
  verification code — the practical substitute for shipping our own
  multi-language SDKs.
- Two secret serializations for one digest must be explained carefully in
  docs; the one-time-secret UX on scheme switch is additional operator
  ceremony, accepted to preserve the "plaintext appears once" rule.
- Migration requires two coordinated steps (receiver upgrades verification,
  then the owner flips the endpoint) instead of one; this is the cost of
  rejecting the mixed-header bridge that the shipped receiver parser would
  have refused.
- The signed content in `standard` binds the event ID into the signature,
  which is a strict security improvement (the spec's dot-injection note);
  our IDs are UUIDs and contain no `.`, and timestamps are integers, so the
  concatenation ambiguity the spec warns about cannot arise.
- Event-type identifiers remain unrestricted 150-char strings; the spec
  recommends `[a-zA-Z0-9_]` dot-delimited identifiers. Tightening validation
  is deliberately out of scope here and would be its own compatibility
  decision.
