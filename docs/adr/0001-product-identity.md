# ADR 0001: Retain the platform identity

- Status: Accepted; external release gates remain open
- Date: 2026-08-24
- Decision owner: repository owner

## Context

The repository contains the control plane, workers, PostgreSQL scheduling,
tenant fairness, retries, dead-letter operations, observability, lifecycle
management, SDK, CLI, and portal. A webhook-only product rename would hide that
system scope. Avoiding Redis or Kafka is a deliberate current architecture, not
a permanent identity constraint.

Package distribution, Python import, and executable names serve different
purposes and do not need to match. Availability checks are point-in-time only
and are not trademark or legal clearance.

## Decision

Retain these identities:

- Repository: `fastapi-microservices-platform`
- Display name: **FastAPI Microservices Platform**
- Python distribution: `fastapi-microservices-platform-sdk`
- Python import package: `webhook_platform_sdk`
- CLI executable: `webhookctl`
- SDK environment prefix: `WEBHOOK_PLATFORM_`
- SDK release tags: `sdk-vMAJOR.MINOR.PATCH`

The platform uses PostgreSQL for transactional state, durable scheduling,
fairness, retries, dead-letter operations, and lifecycle management. Additional
brokers remain deferred until measured throughput, isolation, or retention
requirements trigger the documented adoption gate.

## Consequences

No repository migration or source compatibility alias is required. Before
trusted-publisher configuration, recheck distribution availability and complete
an owner or qualified legal/name review. Publication remains blocked by the
real 8-of-10 usability study, signed-release controls, TestPyPI cooling-off,
protected environments, and hosted validation. This ADR claims none of those
external results or configurations.
