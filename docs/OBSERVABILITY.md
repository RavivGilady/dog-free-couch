# Logging, errors, metrics and alerts

## Logs
The server writes one JSON object per line to stdout: `ts`, `level`, `logger`,
`msg`, plus `request_id`, `user_id` (and `device_id` for agents) on anything
logged during a request. Every response carries `X-Request-ID`; send one in
to trace a call end to end, or quote it from a 500 response body to find the
log line. Tokens, passwords and Telegram bot tokens are redacted.

* Every line has a `component` (`http`, `agent_api`, `telegram`, `telegram_bot`,
  `retention`, `app`). Render has no log groups and labels every line
  `type=app`, so filter on the text in the Logs page: `"component": "http"`
  (or `"component": "telegram"`, etc.); combine with the `level` filter for
  errors only. Hide the dashboard's polling with a regex that excludes
  `/api/(devices|stats)`.
* `LOG_LEVEL` (default `INFO`), `LOG_JSON=0` for plain dev logs.
* `docker-compose.yml` rotates container logs (10 MB x 5 files each), so they
  cannot fill the disk.
* **Retained, searchable store:** not bundled. Point the Docker log driver at
  your choice, e.g. Better Stack / Loki (`docker plugin install grafana/loki-docker-driver`
  and set `logging.driver: loki`) or `awslogs` for CloudWatch. This needs an
  account, so it is the one step left to do by hand.

## Error tracking
Set `SENTRY_DSN` (and `RELEASE`, e.g. the git SHA; compose passes it as a
build arg) for the server. For a camera agent set `DFC_SENTRY_DSN`. Events
are tagged with `release` and `component` (`server` / `agent`); cookies,
auth headers and request bodies are stripped.

## Health
* `/healthz` - liveness; no dependencies. Used by the container healthcheck.
* `/readyz` - readiness; checks the database and media store, 503 if either fails.

## Metrics
`/metrics` (Prometheus format) is **off** unless `METRICS_TOKEN` is set, then
requires `Authorization: Bearer <token>`. Exposes request count and latency
by route/status class, `dfc_agents_online/total`, `dfc_events_ingested_total`,
`dfc_upload_failures_total`, `dfc_disk_used_ratio`, `dfc_db_up`, and
`dfc_backup_last_success_timestamp` (present once a backup job touches
`$DATA_DIR/backup_last_success`).

## Alerts
`deploy/monitoring/` has a Prometheus config and `alerts.yml` covering: site
down, 5xx spike, no agent heartbeats, upload failures, disk > 80%, TLS expiry
< 14 days, and a stale backup. Run Prometheus + blackbox exporter + Alertmanager
(route to email/Slack/PagerDuty) next to the stack, or load `alerts.yml` into
Grafana Cloud. For a zero-setup external check, also point UptimeRobot /
Better Stack at `https://DOMAIN/readyz`.

### Running the monitoring stack safely
* Prometheus and Alertmanager have no authentication: do not publish their
  ports on the internet (bind to localhost or keep them on the private network).
* The `site` job needs a blackbox exporter (`blackbox:9115`, module
  `https_2xx`) that this repo does not ship; until it runs, `SiteDown` fires
  via `absent(probe_success{job="site"})` rather than staying silent.
* Keep the `metrics_token` file mode 0600.
* `dfc_backup_last_success_timestamp` is `0` until a backup has ever run, so
  `BackupStale` fires when backups are not set up.
* `/readyz` is cached for 5 seconds; `LOG_JSON=0` gives plain-text logs (the
  level still applies).
