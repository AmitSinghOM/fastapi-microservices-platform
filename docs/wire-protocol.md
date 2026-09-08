# Webhook wire protocol

This contract is language-neutral. SDKs are conveniences; receivers can
implement the bytes, headers, and HMAC rules directly.

## Event acceptance

`POST /v1/events` requires `X-API-Key`, `Idempotency-Key`, and JSON:

```json
{"type":"order.created","payload":{"order_id":"42"},"envelope_mode":"native"}
```

`envelope_mode` is optional and defaults to `native`. A `202` means the event
and initial fan-out committed atomically, not that a receiver processed it.
Within a project, an idempotency key names one immutable combination of type,
payload, and envelope mode. Repeating that combination returns the original
event. Reusing the key with changed content or mode returns `409`.

Producers should create a stable key from their domain operation, store it with
the operation, and reuse it after timeouts or ambiguous transport failures.
They must not generate a new key for each HTTP attempt.

## Native envelope

The default body remains compact canonical UTF-8 JSON with lexicographically
sorted keys, no insignificant whitespace, and no NaN or infinity:

```json
{"created_at":"<ISO-8601>","data":<payload>,"id":"<event-id>","type":"<type>"}
```

Its media type is `application/json`. The platform stores these exact bytes at
acceptance; retries and explicit replay do not reserialize them.

## Optional CloudEvents structured envelope

Set `envelope_mode` to `cloudevents` for a CloudEvents 1.0-compatible structured
JSON body:

```json
{"data":<payload>,"datacontenttype":"application/json","id":"<event-id>","source":"urn:webhook-platform:project:<project-id>","specversion":"1.0","time":"<ISO-8601>","type":"<type>"}
```

Its media type is `application/cloudevents+json`. This is additive: it does not
change native events, header names, event identity, signature construction, or
retry behavior. Changing only the mode while reusing an idempotency key is a
content conflict.

## Delivery headers and signature

Every attempt sends:

| Header | Meaning |
| --- | --- |
| `Webhook-Id` | Stable event ID; receiver deduplication key |
| `Webhook-Event` | Event type |
| `Webhook-Attempt` | Delivery attempt number |
| `Webhook-Timestamp` | Unix seconds used by the signature |
| `Webhook-Signature` | Scheme-dependent; see below |

Each endpoint selects one of two signature schemes (see
[ADR 0002](adr/0002-standard-webhooks-alignment.md)). The scheme is
snapshotted per delivery at acceptance, so endpoint edits never change the
signature of accepted or replayed work.

### `legacy` scheme (default)

The header is exactly `t=<seconds>,v1=<lowercase-hex>`. For timestamp `T`,
body bytes `B`, and endpoint secret string `S`
(`whsec_` + unpadded base64url of the derived key):

```text
signed = ASCII(decimal(T)) || "." || B
v1 = lowercase_hex(HMAC-SHA256(UTF8(S), signed))
```

A receiver must read the exact raw body before JSON parsing, parse `t` and one
or more `v1` values while ignoring unrecognized space-delimited tokens,
enforce a local timestamp tolerance, recompute the digest over the original
bytes, and compare in constant time. It should also require
`Webhook-Timestamp` to equal `t` and require signed body `id`/`type` fields
to match `Webhook-Id`/`Webhook-Event`.

### `standard` scheme (Standard Webhooks)

The header is `webhook-signature: v1,<base64>` per the
[Standard Webhooks specification](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md);
`webhook-id` and `webhook-timestamp` carry the event ID and unix seconds.
For event ID `I`, timestamp `T`, body bytes `B`, and raw derived key bytes
`K` (the base64-decoded content of the secret after `whsec_`):

```text
signed = ASCII(I) || "." || ASCII(decimal(T)) || "." || B
v1 = standard_base64(HMAC-SHA256(K, signed))
```

Any Standard Webhooks verification library verifies these deliveries using
the standard-form secret (`whsec_` + padded standard base64). Receivers on
the platform SDK auto-detect the scheme from the header token shapes.

Both secret serializations encode the same derived key. The one-time secret
returned by endpoint creation, scheme change, and rotation uses the
endpoint's current scheme's serialization.

### Golden vectors

With signing key `"w" * 32`, endpoint public ID
`11111111-2222-3333-4444-555555555555`, secret version `1`, event ID
`ev-1`, timestamp `1767225600`, and body:

```json
{"created_at":"2026-01-01T00:00:00+00:00","data":{"n":1},"id":"ev-1","type":"order.created"}
```

| Scheme | Value |
| --- | --- |
| `legacy` secret | `whsec_vTRXG2CwqFf6P80ez648Z8G7XaZ43olDednJZdwY4_Y` |
| `legacy` header | `t=1767225600,v1=6b03e0ceb6090c37d1282ff13ddfed9448c36cdd0ad0dd1f5595fcfae95f450d` |
| `standard` secret | `whsec_vTRXG2CwqFf6P80ez648Z8G7XaZ43olDednJZdwY4/Y=` |
| `standard` header | `v1,iH/jZ1lyFWRV9aCjqdIBDtxT1BlnAWLzUAmn+fLcBU0=` |

These vectors are regression-locked in `app/tests/test_signature_scheme.py`,
and `standard` emission is additionally cross-verified by the official
`standardwebhooks` Python library in CI.

During signing-secret rotation, a receiver may test the bounded active and
previous secrets. It must not accept retired secrets indefinitely.

## At-least-once receiver behavior

The same event can arrive more than once, including after a receiver returned
2xx but the worker crashed before finalization. Receiver handling must therefore
be idempotent:

1. Verify timestamp and signature before parsing or storing content.
2. Reserve `Webhook-Id` in durable storage using a unique constraint.
3. Commit the reservation in the same transaction as durable acceptance or
   application state changes.
4. Return 2xx only after that transaction commits.
5. Return 2xx for an already accepted, correctly signed event.
6. Move expensive processing behind a durable local inbox when appropriate.

Automatic attempts retain the same event ID. Explicit platform replay also
retains the event ID, so a receiver that already completed it may intentionally
recognize the replay as a duplicate.

## Response and retry contract

Any 2xx status is success. Network failures, timeouts, `408`, `425`, `429`, and
`5xx` are retryable within platform attempt, age, token-bucket, and circuit
budgets. Other 4xx statuses are terminal. A valid `Retry-After` can delay the
next attempt but is bounded by platform configuration. Receiver response bodies
are not protocol messages and are captured only as a bounded operational prefix.

The service never promises exactly-once delivery or receiver ordering. See
[delivery semantics](delivery-semantics.md) for leases, crash windows, and
replay state transitions.
