# Railway + Vercel private-alpha deployment plan

Prepared 2026-09-03 against M7 commit `8bf0fbd`. Preparation only: no services,
provider accounts, DNS, deployment, or billing changes have been performed.
Use this plan instead of the generic deployment topology for this host split.

## Decisions and audit findings

- Keep Vercel for Next.js/BFF and seven Railway services: Django, HTTP worker,
  Browser/FLOW worker, notification worker, Beat, PostgreSQL, Redis.
- **Trial blocker:** Railway currently documents five services per trial project
  and 1 GB RAM. The requested seven-service topology does not fit that limit.
  Confirm the account can support this layout and browser memory before deploying.
  Limited Trial also restricts outbound networking, which can prevent monitoring,
  email, and object storage from working. Do not silently split projects, merge
  workers, or purchase an upgrade. [Railway trial](https://docs.railway.com/pricing/free-trial)
- **Routing correction:** `.railway.internal` is reachable within the Railway
  environment, not from ordinary Vercel functions. Use a Railway-generated public
  HTTPS API origin as Vercel's server-only `DJANGO_API_URL`. This is not a
  network-private API and is not technically restricted to only the BFF. JWT,
  owner scoping, throttles and registration gating still apply; hiding the URL
  is not access control. Strict BFF-only ingress would require a separate approved
  gateway/network design. No such redesign is included.
  [Railway private networking](https://docs.railway.com/networking/private-networking)
- **Execution detail:** `MonitorViewSet.run` executes synchronously in Django.
  Scheduled checks use Celery. Both Django and the browser worker need Chromium
  and S3 write access. Browser worker concurrency 1 is not a global browser limit;
  manual runs can launch browsers in Django too. Execution semantics are unchanged.
- No PostgreSQL URL parser is necessary: map Railway's separate PG variables to
  existing `DB_*` settings. Existing Celery Redis support is sufficient.
- M7's generic storage settings were present, but the S3 adapter was not installed.
  Added `django-storages[s3]`; no object-storage provider selected.
- Added WhiteNoise and `STATIC_ROOT` for Django admin/DRF assets. Railway is not
  being asked to serve Django static files itself. Evidence is never static content.
- Added an exact liveness-only HTTPS-redirect exception for container health probes.
  Readiness and every other route retain HTTPS enforcement.
- Added a shared Dockerfile and allowlisted build context. Also unignored the
  placeholder frontend `.env.example`, which previously existed only locally.

## Build strategy

Use the root **Dockerfile for all five Python services**, with repository root
as the Railway service root. No Railpack/Nixpacks build and no frontend build on
Railway. Vercel uses root directory `frontend`, its Next.js preset, and the existing
pnpm lockfile/package-manager version. Use a Node runtime supported by that pinned
Next.js version; validate the Vercel build before release.

The common image avoids drift between the two places that launch Chromium.
HTTP, notification and Beat services carry the browser binaries but do not launch
them in their normal jobs. This increases image/build size, not automatically their
idle RAM usage. Splitting images can be revisited after measured need; it is not
required for this alpha. Railway detects a root Dockerfile automatically.
[Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles)

Image steps:

1. Python 3.12 on Debian Bookworm slim; install existing pinned requirements plus
   WhiteNoise 6.12.0 and django-storages 1.14.6 with S3 support.
2. Install `tini`, then `python -m playwright install --with-deps chromium`.
   This uses the repository's pinned Playwright version to choose matching browser
   binaries and Debian packages; do not substitute a mismatched browser image/tag.
3. Copy only backend application/build inputs. `.env`, credentials, media, logs,
   caches, Git history, local virtualenvs and frontend files stay outside the image.
4. Collect admin/DRF static files into the image using development build defaults,
   with no production secrets and no DB/Redis/S3 connection. No migrations at build.
5. Default runtime environment to production, run as non-root UID 10001, use `tini`
   to reap subprocesses. Runtime variables must satisfy production validation.

Chromium requires Linux libraries such as NSS/NSPR, ALSA, ATK/AT-SPI, X11/XCB,
GBM/DRM, Pango/Cairo, fonts and certificate authorities. The pinned Playwright
installer supplies the exact distro package set; avoid a hand-maintained partial
list or Alpine/musl. Its official container guidance also discusses non-root users,
shared memory and sandbox permissions. [Playwright Docker](https://playwright.dev/python/docs/docker)

SSRF code/launch flags are untouched: DNS/IP validation and pinning, redirect and
subresource validation, WebSocket gating, blocked service workers/background
networking and FLOW destination rules remain. Non-root is not proof that Chromium's
OS sandbox is enabled: the existing launch code does not explicitly enable it.
Do not add `--no-sandbox`, privileged mode or network bypasses to solve a deployment
failure. Validate actual Railway isolation/Chromium behavior on controlled targets
before onboarding. A hardened hostile-web sandbox is not claimed by this image.

Docker Desktop is currently unavailable locally. Linux image build and Chromium
startup on Railway remain release gates, not already-verified compatibility claims.

## Exact service settings and commands

Use one replica of each service. Disable sleeping for this always-on monitoring
topology. Do not configure HTTP healthchecks/public domains on workers or Beat.
Apply a restart-on-failure policy and inspect repeated restarts rather than hiding
an OOM loop. Choose the same Railway region for the seven services and a nearby
Vercel function region.

Railway Docker custom start commands replace the image entrypoint; therefore the
commands below explicitly include `tini`. Shell expansion of `$PORT` needs the
shown shell wrapper. Alternatively leave Django's start override empty to use the
equivalent image CMD. [Start commands](https://docs.railway.com/deployments/start-command)

### Django API

```sh
/usr/bin/tini -- /bin/sh -c "exec gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 180 --graceful-timeout 180 --error-logfile -"
```

Only Django's **pre-deploy command**:

```sh
python manage.py migrate --noinput
```

Take/verify a DB backup before upgrades. Pre-deploy runs after build but before
the new API starts, with the runtime environment/private network. Do not put
`collectstatic` here: pre-deploy filesystem writes do not become runtime image
files. Do not run migrations from every worker or every web replica.
[Pre-deploy behavior](https://docs.railway.com/deployments/pre-deploy-command)

- Bind Railway `PORT`; no hardcoded production port needed.
- Healthcheck path `/api/health/`, allow host `healthcheck.railway.app`. It returns
  200 without DB, Redis or browser work, even on container HTTP.
- `/api/readiness/` requires `X-Uptora-Readiness-Key`; missing/wrong key gets 404,
  dependency failure gets generic 503. It checks `SELECT 1` then Redis ping. Redis
  has one-second connect/read timeouts; the DB uses its driver connection settings,
  not a separate endpoint-wide deadline. Use an external request timeout when probing.
- Railway's built-in healthcheck is a rollout check, not ongoing monitoring or a
  protected-readiness substitute. [Healthchecks](https://docs.railway.com/deployments/healthchecks)
- Enable trusted proxy mode only behind Railway's edge and verify forwarded scheme/
  host behavior in smoke. Keep SSL redirect/secure cookies on. Begin HSTS at 0,
  then increase after verifying HTTPS; subdomain/preload remain deliberate choices.
- `/static/` is served by WhiteNoise from the image. `/admin/` stays staff-only;
  allow its HTTPS origin for CSRF. Never expose `/media/` or enable whole-API caching.
- Do not enable Gunicorn raw access logs: public share paths contain capabilities.
  Check Railway/Vercel edge-log redaction separately.

### HTTP worker

```sh
/usr/bin/tini -- celery -A config worker --loglevel=INFO --queues=http --concurrency=2 --hostname=http@%h
```

Queue `http`, prefork pool, concurrency 2. Mostly waiting on network I/O; maintain
existing late acknowledgements and prefetch multiplier 1.

### Browser/FLOW worker

```sh
/usr/bin/tini -- celery -A config worker --loglevel=INFO --queues=browser --concurrency=1 --max-tasks-per-child=20 --hostname=browser@%h
```

Queue `browser` for both Browser and FLOW. Concurrency 1; child recycling limits
long-lived process accumulation without changing task semantics. Start with
roughly **2 GB RAM headroom and one available CPU**. A simple single page may run
within 1 GB, but that is a tight practical lower bound, not a reliable universal
minimum. Real pages vary substantially; measure peak RSS/OOM events. Never shrink
below measured peaks just to fit trial limits. API manual browser runs need similar
headroom and must also be measured.

### Notification worker

```sh
/usr/bin/tini -- celery -A config worker --loglevel=INFO --queues=notifications --concurrency=1 --hostname=notifications@%h
```

Queue `notifications`. Low volume/I/O-bound; only this service needs the real
email API key. Use the existing provider and verified sender.

### Celery Beat

```sh
/usr/bin/tini -- celery -A config beat --loglevel=INFO --schedule=/tmp/uptora-celerybeat-schedule --pidfile=
```

**Exactly one Beat, with no `worker -B` elsewhere.** The local schedule file is
disposable; authoritative monitor/delivery state is in PostgreSQL. No Beat volume
needed. Stop the old Beat before replacement so a rolling overlap cannot create
two schedulers. Replica count 1 alone does not prevent deployment overlap.

## PostgreSQL and Redis mapping

Example service names below are `Postgres` and `Redis`; substitute actual names in
Railway reference variables. Use same-project private connections, not public TCP
proxies. Do not copy credentials into source or give DB/Redis credentials to Vercel.

| Uptora variable (all five Python services) | Railway reference |
| --- | --- |
| `DB_NAME` | `${{Postgres.PGDATABASE}}` |
| `DB_USER` | `${{Postgres.PGUSER}}` |
| `DB_PASSWORD` | `${{Postgres.PGPASSWORD}}` |
| `DB_HOST` | `${{Postgres.PGHOST}}` (verify private host) |
| `DB_PORT` | `${{Postgres.PGPORT}}` |
| `CELERY_BROKER_URL` | `${{Redis.REDIS_URL}}` (verify private host, database 0) |

These maps need **no application change and no database URL parser**. Merely setting
`DATABASE_URL` will not configure current Django settings. If a future convention
requires only that URL, add a parser then; do not invent one now.
[PostgreSQL variables](https://docs.railway.com/databases/postgresql),
[Redis variables](https://docs.railway.com/databases/redis)

Railway Redis is compatible with the existing `celery[redis]` broker. Use a dedicated
broker service, keep result backend disabled, verify `maxmemory-policy=noeviction`,
and leave RAM headroom. Internal Railway traffic is encrypted by its private-network
transport; use the actual supplied URL scheme, not a fabricated `rediss://` endpoint.
If using public connections later, explicitly validate TLS settings first.

Redis is non-authoritative but losing it can delay checks. Restart Redis/workers/Beat
and verify durable dispatcher/stale-run recovery. Configure PostgreSQL backups and
test restoration; Railway database templates are operator-maintained, not an excuse
to omit backups. Preserve evidence separately from DB backups.

## Evidence: external private S3-compatible bucket

The current generic `STORAGES` mapping accepts `EVIDENCE_STORAGE_BACKEND` plus an
options JSON object. The adapter and boto3 are now installed. No provider, account,
bucket, endpoint or credentials have been selected or created.

Required backend: `storages.backends.s3.S3Storage`. Supply provider-selected values
later, for example this **placeholder** options object (not usable credentials):

```json
{"bucket_name":"REPLACE_WITH_PRIVATE_BUCKET","endpoint_url":"https://REPLACE_WITH_S3_ENDPOINT","region_name":"REPLACE_WITH_REGION","default_acl":null,"querystring_auth":true,"file_overwrite":false,"signature_version":"s3v4","object_parameters":{"CacheControl":"private, no-store"}}
```

Use `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as separate secret variables;
add `AWS_SESSION_TOKEN` only for temporary credentials. Do not put keys in JSON,
NEXT_PUBLIC variables, image build args or repository files. The AWS-prefixed names
are adapter conventions, not a choice of AWS as provider. Provider-specific
`addressing_style` may be required. Keep TLS verification enabled.

Grant narrowly scoped bucket/prefix read/write permissions to Django and the browser
worker. No public bucket, public ACL, website hosting, or custom public evidence
domain. Deny anonymous reads at the bucket policy/account level; `default_acl=null`
does not override an accidentally public bucket policy. Storage URLs are not sent
directly to users: existing owner/share-scoped Django views authorize and stream
the file through the BFF. No browser-to-bucket CORS setup is needed for that path.
[S3 adapter configuration](https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html)

Validate write from the scheduled browser worker and read from a different API
container, then restart the API and confirm evidence persists. Validate denied
anonymous bucket reads and owner/share permissions. Do not set
`UPTORA_ALLOW_LOCAL_EVIDENCE_STORAGE=true` on Railway. No local-volume fallback.

## Complete deployment environment matrix

`ALL` means Django, HTTP worker, Browser/FLOW worker, notification worker and Beat.
All load the same production settings, so some common validation variables are
required even where not actively used. Values in this table are a deployment
profile, not changes to convenient local `.env.example` defaults.

| Variable | Services | Value / purpose | Classification / source |
| --- | --- | --- | --- |
| `UPTORA_ENVIRONMENT` | ALL | `production` (also image default) | Manual, nonsecret |
| `DJANGO_DEBUG` | ALL | `False` | Manual, nonsecret |
| `DJANGO_SECRET_KEY` | ALL | Same strong random secret across release services | Manual secret |
| `DJANGO_ALLOWED_HOSTS` | ALL | API Railway hostname and `healthcheck.railway.app`; no wildcard/scheme | Railway hostname + manual nonsecret |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | ALL | `https://uptora.xyz,https://<API-host>` | Manual, public origins |
| `APP_BASE_URL` | ALL | `https://uptora.xyz` | Manual, public frontend origin |
| `UPTORA_READINESS_TOKEN` | ALL | Random operator key; actively used by Django only | Manual secret |
| `UPTORA_REGISTRATION_ENABLED` | ALL | `False`; brief supervised test window only | Manual, nonsecret |
| `DB_NAME`, `DB_USER`, `DB_HOST`, `DB_PORT` | ALL | PG reference mapping above | Railway-provided, internal config |
| `DB_PASSWORD` | ALL | PG reference mapping above | Railway-provided secret |
| `CELERY_BROKER_URL` | ALL | Redis reference above, includes credentials | Railway-provided secret |
| `NOTIFICATIONS_EMAIL_PROVIDER` | ALL | `notifications.email.factories.resend_provider` | Manual, nonsecret |
| `DEFAULT_FROM_EMAIL` | ALL | Existing provider's verified sender | Manual, public sender |
| `RESEND_API_KEY` | Notification worker only | Existing email provider key | Manual secret |
| `UPTORA_ALLOW_CONSOLE_EMAIL` | ALL | `False` | Manual, nonsecret |
| `EVIDENCE_STORAGE_BACKEND` | ALL | `storages.backends.s3.S3Storage` | Manual, nonsecret |
| `EVIDENCE_STORAGE_OPTIONS_JSON` | ALL | Selected private bucket/options, no keys | Manual, internal nonsecret config |
| `UPTORA_ALLOW_LOCAL_EVIDENCE_STORAGE` | ALL | `False` | Manual, nonsecret |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Django, Browser/FLOW worker | Bucket-scoped credentials | Manual secrets from selected provider |
| `AWS_SESSION_TOKEN` | Same, only if temporary credentials | Token with appropriate rotation | Conditional manual secret |
| `AWS_EC2_METADATA_DISABLED` | Django, Browser/FLOW worker | `true`; avoid credential metadata discovery | Manual, nonsecret |
| `DJANGO_TRUST_PROXY_HEADERS` | Django | `True`; verify edge behavior | Manual, nonsecret |
| `DJANGO_SECURE_SSL_REDIRECT` | ALL | `True` | Manual, nonsecret |
| `DJANGO_SESSION_COOKIE_SECURE`, `DJANGO_CSRF_COOKIE_SECURE` | ALL | `True` | Manual, nonsecret |
| `DJANGO_SECURE_HSTS_SECONDS` | Django | `0` initially, raise after TLS verification | Manual, nonsecret |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_SECURE_HSTS_PRELOAD` | Django | `False` initially | Manual, nonsecret |
| `UPTORA_LOG_LEVEL` | ALL | `INFO` | Manual, nonsecret |
| `UPTORA_DISPATCH_INTERVAL_SECONDS` | ALL, used by Beat | `30` (not a monitor interval) | Manual, nonsecret/default |
| `UPTORA_NOTIFICATION_DISPATCH_INTERVAL_SECONDS` | ALL, used by Beat | `60` | Manual, nonsecret/default |
| `PORT` | Django | Platform-injected listening port | Railway-provided, nonsecret |
| `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `PIP_NO_CACHE_DIR` | ALL | `1`, already in image | Image config, nonsecret |
| `PLAYWRIGHT_BROWSERS_PATH` | ALL | `/ms-playwright`, already in image | Image config, nonsecret |
| `NODE_ENV` | Vercel frontend | `production`, platform-managed | Vercel-provided, nonsecret |
| `DJANGO_API_URL` | Vercel frontend/BFF only | `https://<API-host>`; no trailing slash | Manual, server-only origin, not a credential |
| `NEXT_PUBLIC_APP_URL` | Vercel frontend | `https://uptora.xyz`; no trailing slash | Manual, public |
| `NEXT_PUBLIC_APP_NAME` | Vercel frontend | `Uptora` | Manual, public |
| `NEXT_PUBLIC_FEEDBACK_EMAIL` | Vercel frontend | Chosen alpha feedback address | Manual, public |

No SMTP variables are used by the current Resend provider. No `DATABASE_URL`,
`CELERY_RESULT_BACKEND`, object-storage keys, JWT secret or Redis credentials go
to Vercel. Do not use `EVIDENCE_MEDIA_ROOT` for production persistence. No Railway
Docker path variable is required because the Dockerfile is at repository root.

## Domain routing and Vercel boundaries

- `uptora.xyz` routes to Vercel; choose one canonical origin. Redirect alternate
  hostnames to it rather than weakening the BFF's exact Origin check.
- `APP_BASE_URL=https://uptora.xyz` controls backend-generated frontend links.
  `NEXT_PUBLIC_APP_URL=https://uptora.xyz` controls BFF Origin validation.
- `DJANGO_API_URL=https://<API-host>.up.railway.app` is Vercel server-only. Do not
  create `NEXT_PUBLIC_DJANGO_API_URL`, client-side direct fetches or broad CORS rules.
- DB, Redis, workers and Beat get no public networking. Django needs public TLS
  reachability for Vercel, plus staff-only admin for the operator.
- Vercel root `frontend`; install with the pinned pnpm version/frozen lockfile,
  build with `pnpm build`. No Python/Playwright on Vercel. Confirm Fluid Compute
  enabled and the BFF function duration at 300 seconds in project settings; do
  not assume a legacy short function limit can handle manual checks. No frontend
  code changes are needed for this setting. [Vercel function limits](https://vercel.com/docs/functions/limitations)
- The BFF buffers evidence. Vercel documents a 4.5 MB function payload limit;
  existing viewport PNG screenshots should be measured against it during smoke.
  Do not bypass authentication or make the bucket public to solve a limit failure.
  Long multi-step FLOW runs may also exceed function duration; restrict alpha
  flows to small controlled forms. A duration setting is not a universal bound on
  every possible user-configured FLOW. A timeout is ambiguous: inspect results
  before retrying a submission. [Payload limit](https://vercel.com/docs/errors/FUNCTION_RESPONSE_PAYLOAD_TOO_LARGE)
- Production Origin is canonical. Preview deployments should not silently share
  production cookies/DB access; use isolated preview settings or leave previews
  without backend access. Rebuild after changing NEXT_PUBLIC values.

## Resource and trial-cost plan

These are engineering starting estimates, not measured Railway billing or hard
minimums. Limits are ceilings, not predictions of idle usage. Measure actual CPU,
RSS and egress before inviting more testers.

| Railway service | Relative use | Initial practical headroom / rationale |
| --- | --- | --- |
| Django | Moderate; high during manual Browser/FLOW | Around 2 GB for manual browser use; one worker/two threads |
| HTTP worker | Low | Around 512 MB; two I/O-bound task processes |
| Browser/FLOW worker | High | Around 2 GB; one Chromium check at a time |
| Notification worker | Low | 256-512 MB; one I/O-bound task process |
| Beat | Very low | Around 256 MB; one scheduler process |
| PostgreSQL | Moderate | 512 MB-1 GB starting headroom plus persistent disk/backups |
| Redis | Low | 256-512 MB with broker queue headroom; no eviction |

Browser CPU/memory, Django manual browsers, and the seven always-on service
baselines are the main credit risks. Evidence downloads traverse Railway to Vercel;
external storage adds its own fees. Use private DB/Redis connections to avoid
unnecessary public egress. Trial credit is for a short validation, not a guaranteed
month of seven-service monitoring.

Alpha-safe **operator choices**, not new enforced product limits:

- Start with 3 testers, one site each; expand to at most 5 testers/10 sites only
  after measuring usage. Initially one HTTP monitor per site and one or two Browser
  monitors total. No scheduled FLOW until consent and submission volume are reviewed.
- HTTP every 600 seconds (144 checks/day/monitor); 300 seconds only where justified.
- Browser every 1800 seconds (48/day); FLOW manual controlled smoke first, then
  at most every 21600 seconds (four actual submissions/day) for suitable test-safe forms.
- Keep 10-second per-step timeout initially; do not raise all timeouts to 30.
- Existing creation default is 300 seconds for every type: explicitly change
  Browser/FLOW intervals when creating them. These recommendations do not silently
  alter existing monitors or provide a quota/billing cap.
- One replica each; HTTP concurrency 2, Browser 1, notification 1. Avoid repeated
  manual Run now clicks; manual browsers are additional to the scheduled worker.
- Set a usage alert and an owner-approved hard spend limit before activation. A
  hard limit takes workloads offline; it protects cost, not monitoring availability.
  Review trial expiry/stateful-volume retention and keep independent backups.
  [Cost controls](https://docs.railway.com/pricing/cost-control)
- To pause tests, stop Beat and monitor workers and disable test monitors. Do not
  expect longer intervals to eliminate the cost of idle always-on services.

## Safe deployment order (future execution only)

1. Verify account service/RAM/outbound limits, approved budget, restore plan and
   repository revision. Keep automatic deployments disabled until intentional.
   Select the external object-store provider separately; create a private bucket/
   credentials only after approval. Evidence validation cannot complete without it.
2. Create Railway PostgreSQL and Redis in one environment/region, verify internal
   connectivity, Redis eviction policy and DB backup/restore procedure.
3. Configure the Django service source/root/Dockerfile, runtime variables, healthcheck
   and public TLS hostname. Keep registration off and no scheduled monitors enabled.
4. Build Django image; collect static files during build. Run **Django pre-deploy
   migrations**, then start the API. A migration failure must stop the rollout.
   Verify liveness, keyed readiness, HTTPS redirect/proxy, admin/static assets.
5. Deploy HTTP, Browser/FLOW and notification workers using the same source revision.
   Verify distinct node names, queue subscriptions, DB/Redis connectivity, Chromium
   startup as non-root, S3 credentials and email-provider configuration.
6. Start one Beat after workers are ready. Verify periodic dispatch and a controlled
   scheduled check; observe resulting MonitorRun and notification dispatcher behavior.
7. Build/deploy Vercel frontend with the BFF origin and canonical public settings.
   If testing on a temporary Vercel origin, temporarily use that exact origin in
   APP_BASE_URL/NEXT_PUBLIC_APP_URL/CSRF settings, then rebuild for the final domain.
8. Add `uptora.xyz` to the Vercel project; only then use the exact DNS records Vercel
   supplies and verify TLS/canonical redirects. No guessed DNS record values.
   Set final frontend/backend public-origin values before the production smoke.
9. Run the smoke below. Close registration, remove/disable controlled smoke monitors,
   and review actual resource usage before admitting the first testers.

For upgrades: backup first, stop Beat and drain/stop workers if migration compatibility
requires it, apply migrations once, deploy compatible API/workers, then resume Beat.
Do not roll back schema blindly; use the M7 runbook and migration reversibility.

## Production smoke acceptance checklist

Use an operator-controlled HTTPS fixture site with no real orders/bookings/submissions.
Do not whitelist private/link-local addresses or submit random public forms.

1. Open a short supervised registration window; register a unique smoke account.
   Confirm automatic login/dashboard, logout, then explicit login. Close the window
   and confirm new registrations are denied while this account still logs in.
2. Create the controlled website and HTTP monitor (600-second interval). Run now;
   verify the result and dashboard counts. Also observe one scheduled HTTP run via
   Celery/Beat, since Run now alone does not validate workers.
3. Create a Browser monitor with expected text deliberately absent from the fixture.
   Confirm a target-failure result, not an Uptora infrastructure error. Exercise
   both manual API execution and a scheduled browser-worker run.
4. Confirm screenshot upload and owner-authenticated evidence display, including
   after API restart. Logged-out/wrong-owner evidence requests must be denied;
   anonymous direct bucket access must fail. Check payload size through Vercel.
5. Save notification preferences to an operator-controlled inbox. Two consecutive
   target failures should open an incident. Inspect event/delivery and confirm the
   actual email arrives from the verified sender; a preference save is not delivery proof.
6. Create a share report. Open in a clean logged-out session: only the snapshot and
   explicitly included evidence should be available. Raw owner evidence stays denied.
   Revoke/regenerate; verify the old capability fails and the new one works.
7. Restore the fixture assertion, confirm two successful observations resolve the
   incident and the recovery notification is delivered. Existing snapshots stay factual.
8. Logout, attempt a protected route, log back in. Confirm Secure/HttpOnly cookies,
   refresh rotation and invalid Origin rejection. Check no API origin/credentials
   or FlowField values appear in browser-visible configuration/activity data.
9. Optional FLOW: only a controlled receiver, explicit confirmation, safe values,
   one submission. Verify the destination policy without broadening it.
10. Review logs/metrics for crashes, OOMs, repeated internal failures, tokens and
    unexpected task volume. Disable smoke monitors and revoke smoke shares. Stop
    rather than weaken privacy/SSRF checks if any release gate fails.

## Validation boundary

Focused local tests cover liveness over container HTTP versus HTTPS enforcement,
trusted-proxy redirect behavior, collected static serving without media exposure,
and S3 adapter/options loading without contacting any provider. Existing security,
evidence, scheduling and auth tests provide regression coverage. Linux Docker build,
real Railway/Vercel integration, real S3 durability and real email delivery require
the later approved deployment smoke. Nothing in this document claims those already ran.

Local preparation verification on 2026-09-03:

- 229 backend tests passed across deployment configuration, alpha readiness,
  health, auth, browser API, incident sharing, SSRF/fetching, scheduling and
  notification delivery. The static test consumes Django's streamed response
  through the test client's normal cleanup path.
- Django system check and migration dry run passed; no migrations needed.
- `pip check`, Ruff lint/format, and tracked/new-file whitespace checks passed.
- `collectstatic` copied 157 assets and post-processed 453 outputs successfully;
  generated files remain ignored, not committed.
- Production `check --deploy` passed with placeholder secrets and only the
  deliberately deferred HSTS subdomain/preload warnings.
- Docker's Linux daemon was unavailable, so no container image was built or run.
  No Railway, Vercel, S3-provider or DNS mutation was made. All preparation changes
  remain local and uncommitted; they are not included in the earlier GitHub push.
