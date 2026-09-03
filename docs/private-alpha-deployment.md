# Private-alpha deployment

For the selected Vercel frontend + Railway backend topology, follow
[railway-private-alpha.md](railway-private-alpha.md). In that topology Vercel
cannot use a Railway-private hostname: its BFF needs a public HTTPS API origin
kept in server-only configuration. The generic single-network plan below does
not override that boundary.

This plan targets 3–5 agencies or freelancers. It uses ordinary Linux services
and managed infrastructure where available; it does not depend on a particular
hosting vendor.

## Recommended topology

Run separate processes from the same reviewed backend release:

| Service | Process | Notes |
| --- | --- | --- |
| Frontend/BFF | `next start` | Public HTTPS entry point. Django stays private. |
| Django API | `gunicorn config.wsgi:application --bind 0.0.0.0:8000` | Private network only; run migrations before replacing instances. |
| HTTP worker | `celery -A config worker -Q http --concurrency=4` | Outbound HTTP checks. |
| Browser/FLOW worker | `celery -A config worker -Q browser --concurrency=1` | One Chromium process at a time for alpha. Scale deliberately. |
| Notification worker | `celery -A config worker -Q notifications --concurrency=2` | Email only. |
| Scheduler | `celery -A config beat --pidfile=` | Exactly one Beat instance. |

Use a managed PostgreSQL database with automated backups, a managed Redis
instance, and S3-compatible object storage for evidence. A single durable Linux
VM is acceptable for the alpha only if PostgreSQL and evidence are backed up
off-host. Redis is a broker/cache, never the source of truth.

The Browser worker needs the Playwright-declared Chromium version and its Linux
system libraries. Build it with `playwright install --with-deps chromium` or use
the matching official Playwright base image. Give it its own process/container,
low concurrency, memory limits, `no-new-privileges`, and no host filesystem
mounts. Do not disable Uptora's DNS, redirect, subresource, WebSocket, or private
address SSRF checks. A network policy blocking link-local/private metadata
destinations is useful defense in depth, not a replacement for application
checks.

## Routing and health

- Route public traffic only to Next.js.
- Allow Next.js to reach Django over the private service network using
  `DJANGO_API_URL`.
- Do not route Django's `/media/` publicly. Evidence is served only by scoped
  views.
- Route `/static/` to the output of `collectstatic` for Django admin assets.
- Use `/api/health/` for process liveness.
- Use `/api/readiness/` with `X-Uptora-Readiness-Key` for bounded PostgreSQL and
  Redis readiness.
- Terminate HTTPS at a trusted reverse proxy and set
  `DJANGO_TRUST_PROXY_HEADERS=true` only when that proxy overwrites
  `X-Forwarded-Proto`.

## Production environment

Backend required values:

| Variable | Production expectation |
| --- | --- |
| `UPTORA_ENVIRONMENT` | `production` |
| `DJANGO_SECRET_KEY` | Unique secret from a secret manager |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | Django private/public hostnames, comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | HTTPS frontend origin(s) |
| `APP_BASE_URL` | Public HTTPS Next.js origin |
| `UPTORA_READINESS_TOKEN` | Random operator-only health-check secret |
| `UPTORA_REGISTRATION_ENABLED` | Normally `False` |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | PostgreSQL connection |
| `CELERY_BROKER_URL` | TLS Redis URL when supported |
| `RESEND_API_KEY` or `NOTIFICATIONS_EMAIL_PROVIDER` | Real email provider |
| `DEFAULT_FROM_EMAIL` | Verified sender |
| `EVIDENCE_STORAGE_BACKEND` | Installed durable storage backend |
| `EVIDENCE_STORAGE_OPTIONS_JSON` | Backend options without embedded secrets where possible |

Frontend required values:

| Variable | Production expectation |
| --- | --- |
| `DJANGO_API_URL` | Private Django origin; production has no localhost fallback |
| `NEXT_PUBLIC_APP_URL` | Public HTTPS Next.js origin used for Origin enforcement |
| `NEXT_PUBLIC_APP_NAME` | Display name, normally `Uptora` |
| `NEXT_PUBLIC_FEEDBACK_EMAIL` | Public alpha feedback address |

Optional security/operations values are documented in `.env.example`, including
secure-cookie overrides, proxy trust, HSTS, log level, dispatcher intervals,
and explicit acknowledgements for console email or local evidence storage.
`NEXT_PUBLIC_*` values are public by definition; never place credentials in
them.

## Release procedure

1. Build immutable frontend and backend/browser-worker artifacts from one commit.
2. Run backend and frontend checks in CI.
3. Back up PostgreSQL and verify the backup timestamp.
4. Run `python manage.py migrate` as a one-off release job.
5. Run `python manage.py collectstatic --noinput` for admin assets.
6. Replace Django and worker instances; start only one Beat.
7. Replace Next.js.
8. Verify liveness, protected readiness, worker pings, Beat dispatch, login, and
   one controlled HTTP monitor.

HSTS starts at zero. After HTTPS and proxy headers are verified, increase
`DJANGO_SECURE_HSTS_SECONDS` gradually. Enable subdomains/preload only when every
relevant hostname is permanently HTTPS.

## Backups and recovery

- PostgreSQL: daily automated backup plus point-in-time recovery when available;
  retain at least 14 days. Test a restore before admitting testers.
- Evidence: enable object versioning or provider durability and lifecycle rules
  consistent with `data-retention.md`.
- Redis: no authoritative backup is required. After Redis loss, restart it,
  workers, and Beat; database dispatchers recover pending work.
- Worker failure: restart the failed worker. Stale MonitorRuns and notification
  deliveries are recovered from PostgreSQL.
- Rollback: stop Beat during the change, deploy the previous application image,
  and only reverse a migration when its migration file explicitly supports it.
  Prefer a forward fix for data migrations. Restore PostgreSQL only for a
  confirmed destructive migration or corruption event.
