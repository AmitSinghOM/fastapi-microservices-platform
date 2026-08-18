# Security Policy

## Supported versions

| Version | Security fixes |
| --- | --- |
| Latest 3.x release | Yes |
| Earlier releases | No |

Until the first tagged release, support applies to the latest commit on `main`.
Operators should track releases and apply security updates promptly.

## Private reporting

Do not open a public issue for a suspected vulnerability. Use the repository's
[private vulnerability reporting form](https://github.com/AmitSinghOM/fastapi-microservices-platform/security/advisories/new).
Include affected versions, impact, prerequisites, reproduction steps, and a
minimal proof of concept. Remove credentials, customer data, and live targets.

Maintainers aim to acknowledge complete reports within five business days,
provide a status update within ten business days, and coordinate disclosure
after a fix is available. These are response targets, not service guarantees.

## Scope

Reports concerning tenant isolation, authorization, API/signing keys, payload
exposure, webhook forgery, SSRF or egress bypass, replay abuse, queue integrity,
lease ownership, dependency compromise, and unsafe deployment defaults are in
scope. Availability-only load reports without a demonstrated security boundary
violation may be handled as ordinary reliability issues.

## Safe research

Use systems and data you own or have explicit permission to test. Do not access
other tenants, persist access, disrupt service, exfiltrate data, or publish an
unfixed issue. Stop testing and report promptly if sensitive data is exposed.

## Disclosure

The project will credit reporters who request attribution, describe impact and
fixed versions in an advisory, and avoid disclosing exploit details before
users have a reasonable opportunity to upgrade.
