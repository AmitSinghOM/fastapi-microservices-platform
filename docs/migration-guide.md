# Migration guide

Migrate incrementally. Keep the existing path available until accepted-event,
receiver-deduplication, and replay evidence is clean. Do not promise exactly
once, and do not add a broker merely to mirror the platform's PostgreSQL queue.

## From an in-process custom worker

1. Inventory event types, payload limits, endpoint ownership, retry classes,
   signing format, timeout bounds, and current dead-letter operations.
2. Create a platform project and one non-critical endpoint. Store the one-time
   producer key and signing secret in a managed secret service.
3. Add receiver raw-byte HMAC verification and a durable unique key on the
   platform `Webhook-Id` before sending production traffic.
4. Map one domain event to `POST /v1/events`. Use the domain operation ID as a
   stable idempotency key; never use a random value generated per HTTP attempt.
5. Dual-submit a non-sensitive sample or shadow stream and compare accepted
   event IDs, payload meaning, receiver outcomes, and latency. Do not invoke
   irreversible receiver side effects from both paths.
6. Move one event type, monitor queue age and failures, then expand gradually.
7. Keep the old worker available for rollback until retained platform deliveries
   have completed or been intentionally canceled.

The platform retries only network/timeouts, `408`, `425`, `429`, and `5xx`.
Translate receivers that currently rely on retries for ordinary 4xx responses
before cutover.

## From a database polling queue

If application state and event intent already commit in one database, retain
that transactional boundary. Replace the custom sender with the SDK outbox
relay or adapt the existing table:

- Persist event type, JSON payload, and one stable idempotency key atomically.
- Claim a bounded batch with row locking and expiring ownership.
- Commit the claim before network I/O.
- Send to the platform outside the transaction.
- Mark sent only when the same lease still owns the row.
- On ambiguous failures or lease recovery, resend with the same key.

Do not delete outbox rows immediately. Retain enough metadata to reconcile
accepted events without retaining sensitive payloads longer than policy allows.

## From a broker or managed queue

The platform replaces outbound scheduling and retry operations, not necessarily
an application's internal event bus. Keep a broker when multiple internal
consumer groups or long independent retention are genuine requirements.
Otherwise, a transactional application outbox can publish directly to the
platform.

For each existing message:

1. Derive the platform idempotency key from the broker message or domain event
   ID, not the delivery-attempt ID.
2. Acknowledge the source message only after the platform returns `202`.
3. After a timeout, redeliver with the same key; the platform returns the
   original event when it already committed.
4. Preserve ordering assumptions explicitly. The platform does not guarantee
   global or per-endpoint ordering.
5. Stop source retries only after backlog and dead-letter reconciliation.

Do not put the endpoint signing secret or producer API key in message payloads.

## Envelope and signature migration

Native mode matches the original platform envelope and remains default. Choose
CloudEvents mode per accepted event only when consumers are ready for the
structured body and `application/cloudevents+json`. The HMAC algorithm and
headers do not change. Reusing an idempotency key while changing modes is a
conflict, so choose the mode before first submission.

Receivers migrating from signatures over parsed or reserialized JSON must switch
to raw bytes. Framework middleware must not consume and replace the body before
verification. During endpoint-secret rotation, deploy verification of both the
active and bounded previous secret before rotating the platform secret, then
remove the previous secret after its reported overlap expires.

## Validation and rollback

Before cutover, prove:

- same-key retries create one platform event;
- changed same-key content returns conflict;
- exact signed bytes verify and altered bytes fail;
- duplicate IDs cause no duplicate side effect;
- receiver 2xx occurs only after durable acceptance;
- dead delivery inspection and replay are understood by operators;
- credentials, payloads, destinations, and response bodies are absent from logs.

Rollback by stopping new platform submissions and restoring the old submission
path. Revision `0008` refuses downgrade while any CloudEvents-mode rows remain,
because the old schema cannot retain their required media-type selector. Retain
or migrate those rows before rollback; do not rewrite accepted canonical bytes.
Do not cancel processing deliveries under active leases as a substitute for
receiver idempotency; an in-flight request may already have succeeded. Retain
reconciliation records across rollback.
