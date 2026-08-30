"""Browser decision logic, driven against a fake session.

No Chromium here. browser.py deliberately contains no Playwright code, so the
whole verdict machine can be exercised in milliseconds. The real driver is
covered separately by tests/test_browser_integration.py.
"""

import socket

import pytest

from monitors import ssrf
from monitors.checks.browser import (
    BrowserFailure,
    BrowserTimeout,
    Diagnostics,
    Navigation,
    NavigationFailed,
    classify_navigation_error,
    request_allowed,
    run_browser_check,
)
from monitors.models import ErrorType

PUBLIC_IP = '93.184.216.34'
URL = 'https://example.com/status'

SCREENSHOT = b'\x89PNG fake bytes'


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Hostnames resolve public; IP literals resolve to themselves."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = host if any(c in host for c in '.:') and host[0].isdigit() else PUBLIC_IP
        if ':' in host:
            address = host
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (address, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)


class FakeSession:
    """Stands in for PlaywrightSession, honouring the same small contract."""

    def __init__(
        self,
        status=200,
        final_url=URL,
        text='Welcome to Example',
        selectors=('#ready',),
        navigate_error=None,
        screenshot=SCREENSHOT,
        screenshot_error=None,
    ):
        self.status = status
        self.final_url = final_url
        self.text = text
        self.selectors = selectors
        self.navigate_error = navigate_error
        self._screenshot = screenshot
        self.screenshot_error = screenshot_error
        self.diagnostics = Diagnostics()
        self.entered = False
        self.closed = False
        self.screenshots_taken = 0

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def factory(self):
        def make(timeout_ms, launch_args=()):
            self.timeout_ms = timeout_ms
            return self

        return make

    def navigate(self, url, timeout_ms):
        if self.navigate_error is not None:
            raise self.navigate_error
        return Navigation(status=self.status, final_url=self.final_url)

    def has_text(self, text):
        return text in self.text

    def wait_for_selector(self, selector, timeout_ms):
        return selector in self.selectors

    def screenshot(self):
        self.screenshots_taken += 1
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return self._screenshot


def check(session, **kwargs):
    kwargs.setdefault('url', URL)
    kwargs.setdefault('timeout_seconds', 10)
    kwargs.setdefault('now', None)
    return run_browser_check(session_factory=session.factory(), **kwargs)


class TestNavigationOutcomes:
    def test_a_rendered_page_succeeds(self):
        session = FakeSession()

        outcome = check(session)

        assert outcome.is_success
        assert outcome.status_code == 200
        assert outcome.error_type is None
        assert outcome.final_url == URL
        assert outcome.response_time_ms is not None

    @pytest.mark.parametrize('status', [200, 201, 204, 302, 399])
    def test_statuses_below_400_are_treated_as_rendered(self, status):
        outcome = check(FakeSession(status=status))

        assert outcome.is_success

    @pytest.mark.parametrize('status', [400, 404, 410, 500, 503])
    def test_error_statuses_fail(self, status):
        outcome = check(FakeSession(status=status))

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.HTTP_ERROR
        assert outcome.status_code == status
        assert str(status) in outcome.error_message

    def test_navigation_timeout_is_classified(self):
        outcome = check(FakeSession(navigate_error=BrowserTimeout('Timeout 10000ms exceeded')))

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BROWSER_TIMEOUT

    def test_navigation_failure_is_classified(self):
        outcome = check(FakeSession(navigate_error=NavigationFailed('net::ERR_FAILED at ...')))

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.NAVIGATION_ERROR

    def test_a_missing_response_object_still_succeeds(self):
        """Some navigations report no response; that is not a failure."""
        outcome = check(FakeSession(status=None))

        assert outcome.is_success
        assert outcome.status_code is None

    def test_the_session_is_always_closed(self):
        session = FakeSession(navigate_error=NavigationFailed('net::ERR_FAILED'))

        check(session)

        assert session.entered
        assert session.closed

    def test_an_unexpected_browser_error_is_normalized(self):
        session = FakeSession(navigate_error=BrowserFailure('the page crashed'))

        outcome = check(session)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BROWSER_ERROR
        assert session.closed

    def test_an_arbitrary_exception_does_not_escape(self):
        session = FakeSession(navigate_error=RuntimeError('something nobody anticipated'))

        outcome = check(session)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BROWSER_ERROR


class TestExpectations:
    def test_expected_text_present_succeeds(self):
        outcome = check(FakeSession(text='Welcome to Example'), expected_text='Welcome')

        assert outcome.is_success

    def test_expected_text_missing_fails(self):
        outcome = check(FakeSession(text='Service unavailable'), expected_text='Welcome')

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.EXPECTED_TEXT_MISSING
        assert 'Welcome' in outcome.error_message

    def test_expected_selector_present_succeeds(self):
        outcome = check(FakeSession(selectors=('#ready',)), expected_selector='#ready')

        assert outcome.is_success

    def test_expected_selector_missing_fails(self):
        outcome = check(FakeSession(selectors=()), expected_selector='#ready')

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.EXPECTED_SELECTOR_MISSING
        assert '#ready' in outcome.error_message

    def test_both_expectations_must_hold(self):
        session = FakeSession(text='Welcome to Example', selectors=('#ready',))

        outcome = check(session, expected_text='Welcome', expected_selector='#ready')

        assert outcome.is_success

    def test_text_is_checked_before_the_selector(self):
        """The first unmet expectation is the one reported."""
        session = FakeSession(text='nothing useful', selectors=())

        outcome = check(session, expected_text='Welcome', expected_selector='#ready')

        assert outcome.error_type == ErrorType.EXPECTED_TEXT_MISSING

    def test_no_expectations_means_rendering_is_enough(self):
        outcome = check(FakeSession(text='', selectors=()))

        assert outcome.is_success

    def test_expectations_are_not_checked_on_an_error_status(self):
        """A 500 is the story; a missing heading on the error page is not."""
        outcome = check(FakeSession(status=500, text=''), expected_text='Welcome')

        assert outcome.error_type == ErrorType.HTTP_ERROR


class TestEvidence:
    def test_a_failed_check_carries_a_screenshot(self):
        session = FakeSession(status=500)

        outcome = check(session)

        assert outcome.screenshot == SCREENSHOT
        assert session.screenshots_taken == 1

    def test_a_successful_check_takes_no_screenshot(self):
        session = FakeSession()

        outcome = check(session)

        assert outcome.screenshot is None
        assert session.screenshots_taken == 0

    def test_a_failed_screenshot_does_not_replace_the_real_error(self):
        session = FakeSession(status=503, screenshot_error=RuntimeError('display gone'))

        outcome = check(session)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.HTTP_ERROR
        assert 'HTTP 503' in outcome.error_message
        assert outcome.screenshot is None

    def test_a_none_screenshot_is_tolerated(self):
        outcome = check(FakeSession(status=404, screenshot=None))

        assert outcome.error_type == ErrorType.HTTP_ERROR
        assert outcome.screenshot is None

    def test_screenshots_are_kept_out_of_the_column_values(self):
        outcome = check(FakeSession(status=500))

        assert 'screenshot' not in outcome.as_result_fields()


class TestDiagnostics:
    def test_console_errors_alone_do_not_fail_a_working_page(self):
        session = FakeSession()
        for index in range(10):
            session.diagnostics.record_console_error(f'TypeError number {index}')

        outcome = check(session)

        assert outcome.is_success
        assert outcome.error_type is None

    def test_page_errors_alone_do_not_fail_a_working_page(self):
        session = FakeSession()
        session.diagnostics.record_page_error('Uncaught ReferenceError: analytics is not defined')

        outcome = check(session)

        assert outcome.is_success

    def test_console_context_is_appended_to_a_real_failure(self):
        session = FakeSession(status=500)
        session.diagnostics.record_console_error('TypeError: cannot read properties of null')

        outcome = check(session)

        assert 'HTTP 500' in outcome.error_message
        assert 'console:' in outcome.error_message

    def test_console_errors_are_capped(self):
        diagnostics = Diagnostics()
        for index in range(50):
            diagnostics.record_console_error(f'error {index}')

        assert len(diagnostics.console_errors) == 5

    def test_page_errors_are_capped(self):
        diagnostics = Diagnostics()
        for index in range(50):
            diagnostics.record_page_error(f'error {index}')

        assert len(diagnostics.page_errors) == 3

    def test_long_diagnostic_lines_are_truncated(self):
        diagnostics = Diagnostics()
        diagnostics.record_console_error('x' * 5000)

        assert len(diagnostics.console_errors[0]) == 200

    def test_multiline_diagnostics_keep_only_the_first_line(self):
        diagnostics = Diagnostics()
        diagnostics.record_console_error('first line\nsecond line\nthird line')

        assert diagnostics.console_errors[0] == 'first line'

    def test_blocked_records_are_capped(self):
        diagnostics = Diagnostics()
        for index in range(50):
            diagnostics.record_blocked(f'http://10.0.0.{index}/', 'private', navigation=False)

        assert len(diagnostics.blocked_requests) == 10

    def test_error_messages_stay_within_the_column(self):
        session = FakeSession(status=500)
        session.diagnostics.record_console_error('y' * 200)
        session.diagnostics.record_page_error('z' * 200)

        outcome = check(session)

        assert len(outcome.error_message) <= 300


class TestErrorClassification:
    @pytest.mark.parametrize(
        ('message', 'expected'),
        [
            ('net::ERR_NAME_NOT_RESOLVED at http://x/', ErrorType.DNS_ERROR),
            ('net::ERR_CONNECTION_REFUSED at http://x/', ErrorType.CONNECTION_ERROR),
            ('net::ERR_CERT_AUTHORITY_INVALID at https://x/', ErrorType.TLS_ERROR),
            ('net::ERR_SSL_PROTOCOL_ERROR at https://x/', ErrorType.TLS_ERROR),
            ('net::ERR_BLOCKED_BY_CLIENT at http://x/', ErrorType.BLOCKED_TARGET),
            ('net::ERR_TIMED_OUT at http://x/', ErrorType.BROWSER_TIMEOUT),
            ('net::ERR_ADDRESS_UNREACHABLE', ErrorType.CONNECTION_ERROR),
            ('apiRequest: getaddrinfo ENOTFOUND nope.example', ErrorType.DNS_ERROR),
            ('apiRequest: connect ECONNREFUSED 1.2.3.4:443', ErrorType.CONNECTION_ERROR),
            ('something entirely unfamiliar', ErrorType.NAVIGATION_ERROR),
        ],
    )
    def test_network_messages_map_to_the_taxonomy(self, message, expected):
        assert classify_navigation_error(message, None) == expected

    def test_our_own_block_outranks_chromiums_report(self):
        diagnostics = Diagnostics()
        diagnostics.record_blocked('http://169.254.169.254/', 'link-local', navigation=True)

        # Chromium only ever says the request was aborted; we know why.
        assert classify_navigation_error('net::ERR_FAILED', diagnostics) == ErrorType.BLOCKED_TARGET

    def test_a_blocked_subresource_does_not_explain_a_navigation_failure(self):
        diagnostics = Diagnostics()
        diagnostics.record_blocked('http://10.0.0.1/img.png', 'private', navigation=False)

        assert (
            classify_navigation_error('net::ERR_NAME_NOT_RESOLVED', diagnostics)
            == ErrorType.DNS_ERROR
        )

    def test_redirect_overflow_is_reported_as_such(self):
        diagnostics = Diagnostics()
        diagnostics.redirect_overflow = True

        assert (
            classify_navigation_error('net::ERR_FAILED', diagnostics)
            == ErrorType.TOO_MANY_REDIRECTS
        )


class TestBrowserSsrfGate:
    """request_allowed is the gate every browser request passes through."""

    @pytest.mark.parametrize(
        'url',
        [
            'http://127.0.0.1/',
            'http://localhost/',
            'http://10.0.0.5/',
            'http://172.16.4.4/',
            'http://192.168.1.1/',
            'http://169.254.169.254/latest/meta-data/',
            'http://[::1]/',
            'http://[fd00::1]/',
            'http://[fe80::1]/',
            'http://0.0.0.0/',
        ],
    )
    def test_internal_targets_are_refused(self, url):
        allowed, reason = request_allowed(url)

        assert not allowed
        assert reason

    @pytest.mark.parametrize(
        'url',
        [
            'ftp://example.com/',
            'file:///etc/passwd',
            'file://C:/Windows/win.ini',
            'ws://example.com/live',
            'wss://example.com/live',
            'gopher://example.com/',
            'ftps://example.com/',
        ],
    )
    def test_non_web_schemes_are_refused(self, url):
        """Only http and https reach the validated fetch path.

        ws and wss matter most here: they are network-capable and Chromium
        opens them itself, so they are refused outright rather than proxied.
        """
        allowed, reason = request_allowed(url)

        assert not allowed
        assert reason

    @pytest.mark.parametrize(
        'url',
        ['data:image/png;base64,AAAA', 'about:blank', 'blob:https://example.com/abc'],
    )
    def test_inert_schemes_are_allowed_without_resolution(self, url):
        """These never leave the process, so there is nothing to validate."""
        allowed, _ = request_allowed(url)

        assert allowed

    def test_a_public_target_is_allowed(self):
        allowed, reason = request_allowed('https://example.com/page')

        assert allowed
        assert reason is None


class TestPreflight:
    """The target is validated before a browser is ever launched."""

    def test_a_private_target_never_starts_a_browser(self, monkeypatch):
        started = {'count': 0}

        def factory(timeout_ms, launch_args=()):
            started['count'] += 1
            raise AssertionError('a blocked target must not launch a browser')

        outcome = run_browser_check(
            url='http://10.0.0.5/', timeout_seconds=10, now=None, session_factory=factory
        )

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BLOCKED_TARGET
        assert started['count'] == 0

    def test_an_unresolvable_target_never_starts_a_browser(self, monkeypatch):
        def fail(*args, **kwargs):
            raise socket.gaierror('Name or service not known')

        monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fail)

        def factory(timeout_ms, launch_args=()):
            raise AssertionError('an unresolvable target must not launch a browser')

        outcome = run_browser_check(
            url='https://nope.example/', timeout_seconds=10, now=None, session_factory=factory
        )

        assert outcome.error_type == ErrorType.DNS_ERROR

    def test_a_navigation_landing_somewhere_private_is_refused(self):
        """Defence in depth: whatever the gate did, the page must not rest there."""
        session = FakeSession(final_url='http://169.254.169.254/latest/')

        outcome = check(session)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BLOCKED_TARGET
        assert outcome.screenshot == SCREENSHOT

    def test_the_configured_timeout_reaches_the_session(self):
        session = FakeSession()

        check(session, timeout_seconds=7)

        assert session.timeout_ms == 7000


class TestForwardedHeaders:
    """Headers relayed on behalf of the browser must not describe our connection."""

    def test_connection_owning_headers_are_dropped(self):
        from monitors.checks.fetching import forwardable_headers

        forwarded = forwardable_headers(
            {
                'host': 'evil.example',
                'content-length': '10',
                'accept-encoding': 'br',
                'connection': 'keep-alive',
                'transfer-encoding': 'chunked',
                'upgrade': 'websocket',
                'user-agent': 'Chrome',
                'accept': 'text/html',
                'cookie': 'session=abc',
            }
        )

        assert forwarded == {
            'user-agent': 'Chrome',
            'accept': 'text/html',
            'cookie': 'session=abc',
        }

    def test_a_forged_host_header_cannot_survive(self):
        """The Host is derived from the validated target, never from the page."""
        from monitors.checks.fetching import forwardable_headers

        assert 'host' not in forwardable_headers({'Host': 'internal.service'})
