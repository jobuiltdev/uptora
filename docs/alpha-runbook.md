# Private-alpha operator runbook

## Deploy and verify

1. Follow `private-alpha-deployment.md`; confirm backups before migrations.
2. Run `python manage.py check --deploy` with production environment values.
3. Run `python manage.py migrate` and `collectstatic --noinput`.
4. Start Django, Next.js, the `http`, `browser`, and `notifications` workers,
   then one Beat process.
5. Check `GET /api/health/` returns `{"status":"ok"}`.
6. Check `GET /api/readiness/` with `X-Uptora-Readiness-Key` returns
   `{"status":"ready"}`. A generic `not_ready` means PostgreSQL or Redis needs
   investigation; details stay in operator logs.
7. Run `celery -A config inspect ping` and confirm every expected worker name.
8. Confirm Beat logs periodic `dispatch_due_monitors` and
   `dispatch_pending_notifications` sends.

## Admit a tester

Registration is disabled by default in production.

- Safest: create the tester in Django admin with a temporary password delivered
  out of band, then have them sign in.
- Controlled window: set `UPTORA_REGISTRATION_ENABLED=true`, deploy/restart
  Django, ask the tester to register, then immediately set it back to `false`.

The backend flag is authoritative; hiding the registration UI is not the
security boundary. Existing users can always log in while registration is off.

## Routine checks

- Notifications: inspect Notification Events and Deliveries in admin. A failed
  delivery shows attempts, last error, and next attempt without exposing auth
  or capability tokens.
- Failed runs: filter MonitorRuns by `FAILED_INTERNAL`; correlate the run,
  monitor, timestamps, and worker log ID. Do not reinterpret it as a target
  outage.
- Incidents: inspect the Incident row and related CheckResults. Two failures
  open an incident and two consecutive successes resolve it.
- Evidence: verify the CheckResult owns a screenshot and storage credentials are
  valid. Never expose `/media/`; use the authenticated or share-scoped endpoint.
- Shares: find IncidentShare in admin for status only. Revoke from the owner's
  incident page; the token is intentionally hidden in admin.

## Recovery

- HTTP worker down: restart it; stale leases are recovered by the dispatcher.
- Browser worker down: restart with Chromium dependencies and concurrency 1.
- Notification worker down: restart it; pending deliveries remain in PostgreSQL.
- Beat down: start exactly one instance. Overdue monitor slots coalesce rather
  than creating a backlog.
- Redis loss: start Redis, then workers and Beat. PostgreSQL remains authoritative.
- PostgreSQL issue: stop Beat and workers, restore service/backup, run readiness,
  then resume workers and Beat.

## Emergency controls

- Disable new accounts: set `UPTORA_REGISTRATION_ENABLED=false` and restart
  Django.
- Stop all monitoring: stop Beat and both monitor workers. Do not delete monitor
  schedules. Notification worker may remain up for already-recorded events.
- Stop browser submissions only: stop the `browser` queue worker; HTTP monitoring
  can continue.
- Revoke a leaked client report: use the incident's Share report section. Do not
  paste the token into tickets or logs.
- Roll back: stop Beat, deploy the prior immutable release, apply only explicitly
  safe reverse migrations, verify readiness, and restart workers.

## Security and privacy reminders

- Never paste JWTs, share URLs, cookies, request bodies, FlowField values, or
  evidence storage paths into logs or support messages.
- Application logging redacts bearer and IncidentShare tokens. Configure edge,
  proxy, and platform access logs to redact `/share/incidents/*` and
  `/api/public/incident-shares/*` too.
- Admin is staff-only behind HTTPS. Prefer additional network/identity controls.
- Password reset is operator-assisted during alpha; no self-service recovery is
  implemented yet.
