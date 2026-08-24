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
| `Webhook-Signature` | `t=<seconds>,v1=<lowercase-hex>` |

For timestamp `T`, body bytes `B`, and endpoint secret `S`:

```text
signed = ASCII(decimal(T)) || "." || B
v1 = lowercase_hex(HMAC-SHA256(UTF8(S), signed))
```

A receiver must read the exact raw body before JSON parsing, parse `t` and one
or more `v1` values, reject malformed headers, enforce a local timestamp
tolerance, recompute the digest over the original bytes, and compare in constant
time. It should also require `Webhook-Timestamp` to equal `t` and require signed
body `id`/`type` fields to match `Webhook-Id`/`Webhook-Event`.

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
