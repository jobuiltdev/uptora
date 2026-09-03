# Shared by Django and all Celery services. Django also needs Chromium because
# manual Browser/FLOW runs execute synchronously in the API (not in Celery).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# The allowlisted build context excludes credentials, media, frontend and caches.
COPY . .
# No database, Redis, object store, or production secrets are used at build time.
RUN python manage.py collectstatic --noinput \
    && useradd --create-home --uid 10001 uptora \
    && chown -R uptora:uptora /app

# Runtime fails closed until real production variables are supplied.
ENV UPTORA_ENVIRONMENT=production
USER uptora
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 2 --timeout 180 --graceful-timeout 180 --error-logfile -"]
