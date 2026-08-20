# Observability deployment artifacts

Mount `prometheus.yml`, `recording-rules.yml`, and `alerts.yml` into a
Prometheus deployment. Import the four JSON files under `grafana/`. API and
worker targets must remain on a private monitoring network; do not publish the
worker metrics port directly to the internet.

Run `postgres-monitoring-role.sql` once with an administrator after replacing
the password placeholder. Configure `postgres_exporter` with that role and its
built-in `stat_database`, `stat_bgwriter`, `stat_user_tables`, `locks`, `wal`,
`database_wraparound`, `long_running_transactions`, and `postmaster`
collectors. The role receives only database CONNECT and `pg_monitor`; never use
application or administrator credentials.

`autoscaling-policy.yaml` is an operator-neutral policy contract, not a custom
resource to apply directly. Translate all four signals into the deployment's
HPA/KEDA/other controller. Preserve its maximum-replica hard budgets and the
ten-minute scale-down stabilization window.

Validate before deployment:

```bash
promtool check config deploy/observability/prometheus.yml
promtool check rules deploy/observability/recording-rules.yml
promtool check rules deploy/observability/alerts.yml
```
