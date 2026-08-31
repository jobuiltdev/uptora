"""The browser check: does the page actually render?

This module decides what a browser observation means. It contains no Playwright
code at all — the driver lives in playwright_session and hands back plain values
and the exceptions defined here, so the decision logic can be exercised against
a fake session without launching Chromium.

Success means the page loaded and rendered: navigation completed, the final
response was not an error status, and every configured expectation was met. A
browser check with no expectations configured is still meaningful — "Chromium
loaded this page without a fatal error" is more than an HTTP check proves.
"""

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from monitors.checks.base import CheckOutcome, summarize
from monitors.models import ErrorType
from monitors.ssrf import (
    ALLOWED_SCHEMES,
    BlockedTargetError,
    TargetResolutionError,
    resolve_target,
)

# Schemes that involve no network at all, so there is nothing to validate.
INERT_SCHEMES = frozenset({'data', 'blob', 'about'})

MAX_CONSOLE_ERRORS = 5
MAX_PAGE_ERRORS = 3
MAX_BLOCKED_RECORDS = 10
DIAGNOSTIC_MESSAGE_LIMIT = 200

SUCCESS_STATUS_CEILING = 400

# Chromium reports network failures as net::ERR_* strings. Mapping the ones we
# can recognise keeps browser failures in the same taxonomy as HTTP failures: a
# DNS failure is the same fact however it was observed.
NET_ERROR_TYPES = (
    ('ERR_NAME_NOT_RESOLVED', ErrorType.DNS_ERROR),
    ('ERR_NAME_RESOLUTION_FAILED', ErrorType.DNS_ERROR),
    ('ERR_BLOCKED_BY_CLIENT', ErrorType.BLOCKED_TARGET),
    ('ERR_CERT', ErrorType.TLS_ERROR),
    ('ERR_SSL', ErrorType.TLS_ERROR),
    ('ERR_CONNECTION_REFUSED', ErrorType.CONNECTION_ERROR),
    ('ERR_CONNECTION_RESET', ErrorType.CONNECTION_ERROR),
    ('ERR_CONNECTION_CLOSED', ErrorType.CONNECTION_ERROR),
    ('ERR_CONNECTION_FAILED', ErrorType.CONNECTION_ERROR),
    ('ERR_ADDRESS_UNREACHABLE', ErrorType.CONNECTION_ERROR),
    ('ERR_CONNECTION_TIMED_OUT', ErrorType.BROWSER_TIMEOUT),
    ('ERR_TIMED_OUT', ErrorType.BROWSER_TIMEOUT),
    ('ERR_TOO_MANY_REDIRECTS', ErrorType.TOO_MANY_REDIRECTS),
    # Navigation hops are fetched by the Playwright driver, which reports Node
    # error codes rather than Chromium's net:: strings.
    ('ENOTFOUND', ErrorType.DNS_ERROR),
    ('EAI_AGAIN', ErrorType.DNS_ERROR),
    ('ECONNREFUSED', ErrorType.CONNECTION_ERROR),
    ('ECONNRESET', ErrorType.CONNECTION_ERROR),
    ('EHOSTUNREACH', ErrorType.CONNECTION_ERROR),
    ('ETIMEDOUT', ErrorType.BROWSER_TIMEOUT),
    ('CERT_', ErrorType.TLS_ERROR),
    ('SELF_SIGNED', ErrorType.TLS_ERROR),
)


class BrowserTimeout(Exception):
    """Navigation or an expectation did not complete within the budget."""


class NavigationFailed(Exception):
    """Chromium could not load the page. Carries the raw net:: message."""


class BrowserFailure(Exception):
    """The browser itself misbehaved: launch failed, page crashed, protocol error."""


class ElementNotFound(Exception):
    """A selector matched nothing within the budget."""


class InteractionFailed(Exception):
    """The element was there but could not be filled, checked or clicked."""


@dataclass
class Navigation:
    """What a completed navigation reported."""

    status: int | None
    final_url: str | None


@dataclass
class Diagnostics:
    """A small, bounded record of what the page complained about.

    Deliberately capped and never written to its own column. Console output is
    unbounded by nature and a monitoring table is the wrong place for it; a
    handful of truncated lines is enough to tell an operator what went wrong.
    """

    console_errors: list = field(default_factory=list)
    page_errors: list = field(default_factory=list)
    blocked_requests: list = field(default_factory=list)
    redirect_overflow: bool = False

    def record_console_error(self, text):
        self._append(self.console_errors, text, MAX_CONSOLE_ERRORS)

    def record_page_error(self, text):
        self._append(self.page_errors, text, MAX_PAGE_ERRORS)

    def record_blocked(self, url, reason, navigation):
        entry = {'url': url[:DIAGNOSTIC_MESSAGE_LIMIT], 'reason': reason, 'navigation': navigation}
        if len(self.blocked_requests) < MAX_BLOCKED_RECORDS:
            self.blocked_requests.append(entry)

    @property
    def blocked_navigation(self):
        """The first blocked top-level navigation, which explains an aborted load."""
        for entry in self.blocked_requests:
            if entry['navigation']:
                return entry
        return None

    def summary(self):
        """One short line of context to append to an error message."""
        parts = []
        if self.console_errors:
            parts.append(f'console: {self.console_errors[0]}')
        if self.page_errors:
            parts.append(f'page error: {self.page_errors[0]}')
        return '; '.join(parts)

    @staticmethod
    def _append(target, text, limit):
        if len(target) >= limit:
            return
        target.append(str(text).strip().splitlines()[0][:DIAGNOSTIC_MESSAGE_LIMIT])


def request_allowed(url):
    """Whether a browser-originated request may be made at all.

    A pre-flight opinion on a single URL, used before launching a browser and to
    re-check where a navigation came to rest. It is not the enforcement point:
    the driver fetches every request itself through the pinned path, which
    re-validates each redirect hop as it goes.

    Returns (allowed, reason).
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme in INERT_SCHEMES:
        # data:, blob: and about: never leave the process.
        return True, None

    if scheme not in ALLOWED_SCHEMES:
        # file:, ftp:, ws:, wss: and anything else that could reach a local
        # resource or open a connection outside the fetch path.
        return False, f'Scheme {scheme!r} is not permitted during a check.'

    try:
        resolve_target(url)
    except (BlockedTargetError, TargetResolutionError) as exc:
        return False, str(exc)
    return True, None


def classify_navigation_error(message, diagnostics):
    """Map a navigation failure onto the stable taxonomy.

    Our own decisions win over Chromium's report, which only ever says the
    request was aborted and cannot say why.
    """
    if diagnostics is not None:
        if diagnostics.blocked_navigation is not None:
            return ErrorType.BLOCKED_TARGET
        if diagnostics.redirect_overflow:
            return ErrorType.TOO_MANY_REDIRECTS

    text = str(message).upper()
    for marker, error_type in NET_ERROR_TYPES:
        if marker in text:
            return error_type
    return ErrorType.NAVIGATION_ERROR


def compose_message(primary, diagnostics):
    """Attach a little page context to an error, without letting it take over."""
    context = diagnostics.summary() if diagnostics is not None else ''
    return summarize(f'{primary} | {context}' if context else primary)


def capture_screenshot(session):
    """Best-effort evidence.

    Never raises. A screenshot is evidence about a failure, not the observation
    itself, so failing to take one must not change or hide what actually went
    wrong with the site.
    """
    try:
        return session.screenshot()
    except Exception:  # noqa: BLE001 - evidence must never displace the finding
        return None


def failed(session, error_type, message, elapsed_ms, **extra):
    """Build a failure outcome, with a screenshot attached if one can be taken."""
    diagnostics = getattr(session, 'diagnostics', None)
    return CheckOutcome.failure(
        error_type,
        compose_message(message, diagnostics),
        response_time_ms=elapsed_ms,
        screenshot=capture_screenshot(session),
        **extra,
    )


def load_page(session, url, timeout_ms, elapsed_ms):
    """Navigate and confirm the page is genuinely there.

    Returns (navigation, failure). Exactly one is None. Shared by the browser
    check and by flows, so "did the page load" means the same thing and is
    classified the same way for both.
    """
    try:
        navigation = session.navigate(url, timeout_ms)
    except BrowserTimeout as exc:
        return None, failed(session, ErrorType.BROWSER_TIMEOUT, exc, elapsed_ms())
    except NavigationFailed as exc:
        error_type = classify_navigation_error(exc, getattr(session, 'diagnostics', None))
        return None, failed(session, error_type, exc, elapsed_ms())

    navigated_ms = elapsed_ms()
    common = {'status_code': navigation.status, 'final_url': navigation.final_url}

    # Defence in depth: whatever the route gate did, the page must not have
    # come to rest somewhere private.
    if navigation.final_url:
        allowed, reason = request_allowed(navigation.final_url)
        if not allowed:
            return None, failed(
                session,
                ErrorType.BLOCKED_TARGET,
                f'Navigation ended at a blocked target: {reason}',
                navigated_ms,
                **common,
            )

    if navigation.status is not None and navigation.status >= SUCCESS_STATUS_CEILING:
        return None, failed(
            session,
            ErrorType.HTTP_ERROR,
            f'HTTP {navigation.status}',
            navigated_ms,
            **common,
        )

    return navigation, None


def inspect(session, url, timeout_ms, expected_text, expected_selector, elapsed_ms):
    """Drive one loaded page to a verdict. `session` may be real or a fake."""
    navigation, failure = load_page(session, url, timeout_ms, elapsed_ms)
    if failure is not None:
        return failure

    common = {'status_code': navigation.status, 'final_url': navigation.final_url}

    if expected_text and not session.has_text(expected_text):
        return failed(
            session,
            ErrorType.EXPECTED_TEXT_MISSING,
            f'Expected text not found on the page: {expected_text!r}',
            elapsed_ms(),
            **common,
        )

    if expected_selector:
        remaining = max(timeout_ms - elapsed_ms(), 0)
        if not session.wait_for_selector(expected_selector, remaining):
            return failed(
                session,
                ErrorType.EXPECTED_SELECTOR_MISSING,
                f'Expected selector not found on the page: {expected_selector!r}',
                elapsed_ms(),
                **common,
            )

    # Console noise and uncaught page errors are recorded but do not fail the
    # check. Real sites log errors constantly; treating that as an outage would
    # bury genuine incidents in false alarms. Revisit once there is somewhere
    # for a "degraded" signal to go.
    return CheckOutcome(is_success=True, response_time_ms=elapsed_ms(), **common)


def default_session_factory(timeout_ms, launch_args=()):
    """Import the Playwright driver lazily, so nothing else pays for it."""
    from monitors.checks.playwright_session import PlaywrightSession

    return PlaywrightSession(timeout_ms=timeout_ms, launch_args=launch_args)


def run_browser_check(
    url,
    timeout_seconds,
    now,
    expected_text=None,
    expected_selector=None,
    session_factory=None,
    launch_args=(),
):
    """Load `url` in a browser and describe what happened.

    `session_factory` and `launch_args` are test seams: the first swaps Chromium
    for a fake, the second lets an integration test point a real Chromium at a
    local fixture server. Production passes neither.
    """
    started = time.perf_counter()

    def elapsed_ms():
        return int((time.perf_counter() - started) * 1000)

    # Checked before launching, so an obviously forbidden target never costs a
    # browser. The route gate re-checks it anyway, along with every redirect.
    try:
        resolve_target(url)
    except BlockedTargetError as exc:
        return CheckOutcome.failure(ErrorType.BLOCKED_TARGET, exc)
    except TargetResolutionError as exc:
        return CheckOutcome.failure(ErrorType.DNS_ERROR, exc, response_time_ms=elapsed_ms())

    factory = session_factory or default_session_factory
    timeout_ms = timeout_seconds * 1000

    try:
        with factory(timeout_ms, launch_args) as session:
            return inspect(session, url, timeout_ms, expected_text, expected_selector, elapsed_ms)
    except BrowserTimeout as exc:
        return CheckOutcome.failure(ErrorType.BROWSER_TIMEOUT, exc, response_time_ms=elapsed_ms())
    except (BrowserFailure, Exception) as exc:  # noqa: BLE001 - a bad page must not kill the runner
        return CheckOutcome.failure(ErrorType.BROWSER_ERROR, exc, response_time_ms=elapsed_ms())
