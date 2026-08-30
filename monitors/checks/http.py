"""The HTTP check.

Everything here is pure: given a URL and a timeout it returns a CheckOutcome. No
database, no Django request, no logging side effects, so the same call works
from a view today and from a Celery task later.

The fetching itself lives in monitors.checks.fetching, which validates and pins
every redirect hop. The browser gate uses the same primitive, so there is one
SSRF implementation rather than two that can drift apart.
"""

import ssl
import time

import httpx

from monitors.checks import tls
from monitors.checks.base import CheckOutcome, summarize
from monitors.checks.fetching import TooManyRedirectsError, fetch_validated
from monitors.models import ErrorType
from monitors.ssrf import BlockedTargetError, TargetResolutionError

USER_AGENT = 'Uptora-Monitor/1.0 (+https://uptora.example)'

# Anything the origin answers with below 400 counts as reachable, redirects
# included: a 3xx that we stopped following is still a live server.
SUCCESS_STATUS_CEILING = 400


def has_tls_cause(exc):
    """True when an ssl error is anywhere in the exception's chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl.SSLError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def classify_transport_error(exc):
    """Map an httpx failure onto the stable ErrorType taxonomy."""
    if isinstance(exc, httpx.TimeoutException):
        return ErrorType.TIMEOUT
    if has_tls_cause(exc):
        return ErrorType.TLS_ERROR
    if isinstance(exc, httpx.TransportError):
        return ErrorType.CONNECTION_ERROR
    return ErrorType.UNKNOWN_ERROR


def collect_ssl_metadata(target, timeout_seconds, now):
    """Certificate expiry for an https target. Empty for plain http, and empty
    (never failing) when the certificate cannot be read."""
    if not target.is_tls:
        return {}
    expires_at = tls.inspect_certificate(
        hostname=target.hostname,
        ip=target.ip,
        port=target.port,
        timeout_seconds=timeout_seconds,
    )
    if expires_at is None:
        return {}
    return {
        'ssl_expires_at': expires_at,
        'ssl_days_remaining': tls.days_remaining(expires_at, now),
    }


def run_http_check(url, timeout_seconds, now, transport=None):
    """Fetch `url` once and describe what happened.

    `transport` is a test seam for httpx.MockTransport; production leaves it
    unset. `now` is passed in so the caller controls the clock the whole check
    is measured against.
    """
    started = time.perf_counter()

    def elapsed_ms():
        return int((time.perf_counter() - started) * 1000)

    client = httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        transport=transport,
    )
    try:
        with client:
            fetched = fetch_validated(client, url, headers={'User-Agent': USER_AGENT})
            response, target, final_url = fetched.response, fetched.target, fetched.final_url
    except BlockedTargetError as exc:
        return CheckOutcome.failure(ErrorType.BLOCKED_TARGET, exc)
    except TargetResolutionError as exc:
        return CheckOutcome.failure(ErrorType.DNS_ERROR, exc, response_time_ms=elapsed_ms())
    except TooManyRedirectsError as exc:
        return CheckOutcome.failure(
            ErrorType.TOO_MANY_REDIRECTS, exc, response_time_ms=elapsed_ms()
        )
    except httpx.HTTPError as exc:
        return CheckOutcome.failure(
            classify_transport_error(exc), exc, response_time_ms=elapsed_ms()
        )

    # Measured before certificate inspection so the extra connection that reads
    # the certificate does not inflate the reported response time.
    response_time_ms = elapsed_ms()

    is_success = response.status_code < SUCCESS_STATUS_CEILING
    ssl_metadata = collect_ssl_metadata(target, timeout_seconds, now)

    return CheckOutcome(
        is_success=is_success,
        status_code=response.status_code,
        response_time_ms=response_time_ms,
        final_url=final_url,
        error_type=None if is_success else ErrorType.HTTP_ERROR,
        error_message=None
        if is_success
        else summarize(f'HTTP {response.status_code} {response.reason_phrase}'),
        **ssl_metadata,
    )
