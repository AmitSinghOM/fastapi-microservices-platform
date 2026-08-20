-- Run as a PostgreSQL administrator after replacing the password placeholder.
-- The exporter role cannot mutate application data.
CREATE ROLE webhook_monitor LOGIN PASSWORD 'REPLACE_WITH_MANAGED_SECRET';
GRANT CONNECT ON DATABASE webhooks TO webhook_monitor;
GRANT pg_monitor TO webhook_monitor;

-- Verify periodically; expected values are false.
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication
FROM pg_roles
WHERE rolname = 'webhook_monitor';
