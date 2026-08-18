# Pull request

## Problem and approach

Describe the user/operator problem and why this approach is appropriate.

## Risk and compatibility

- Security and tenant-isolation impact:
- API/configuration/database compatibility:
- Migration and rollback or fix-forward plan:

## Validation

List exact commands and results. Include live PostgreSQL tests for queue,
transaction, migration, or lease changes.

## Checklist

- [ ] Documentation and release notes are updated.
- [ ] No credentials, payloads, responses, or customer data are included.
- [ ] `make check` passes.
- [ ] PostgreSQL and migration checks pass when applicable.
- [ ] Threat-model changes are documented.
