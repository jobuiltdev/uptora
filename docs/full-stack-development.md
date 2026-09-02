# Full-stack local development

Uptora's browser talks only to the Next.js application. Next.js forwards a
constrained set of requests to Django and stores JWT credentials in HttpOnly
cookies. Django does not need wildcard or browser-facing CORS configuration.
Mutating BFF requests must carry the configured frontend Origin.

## Environment

Backend `.env`:

```text
APP_BASE_URL=http://localhost:3000
```

Frontend `frontend/.env.local` (copy from `frontend/.env.example`):

```text
DJANGO_API_URL=http://localhost:8000
NEXT_PUBLIC_APP_URL=http://localhost:3000
NEXT_PUBLIC_APP_NAME=Uptora
```

## Processes

Start PostgreSQL and Redis, then run:

```text
python manage.py runserver
cd frontend
pnpm dev
celery -A config worker -Q http --concurrency=4
celery -A config worker -Q browser --concurrency=1
celery -A config worker -Q notifications --concurrency=2
celery -A config beat
```

On native Windows use `--pool=solo` for Celery workers. Browser monitoring also
requires the declared Playwright Chromium build.

Failure evidence is deliberately not exposed through `MEDIA_URL`. It is streamed
through the authenticated, owner-scoped check-result evidence endpoint.

Private-alpha deployment, retention, and operator procedures live in:

- `docs/private-alpha-deployment.md`
- `docs/data-retention.md`
- `docs/alpha-runbook.md`
