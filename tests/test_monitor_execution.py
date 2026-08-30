"""Execution engine tests.

No real network or DNS is involved: httpx.MockTransport answers every request
and socket.getaddrinfo is stubbed to a fixed public address.
"""

import socket
import ssl
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from monitors import ssrf
from monitors.checks import tls
from monitors.execution import execute_monitor
from monitors.models import CheckResult, ErrorType

pytestmark = pytest.mark.django_db

PUBLIC_IP = '93.184.216.34'


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Resolve every hostname to one public address."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)


@pytest.fixture(autouse=True)
def no_certificate(monkeypatch):
    """Certificate inspection is off unless a test opts in."""
    monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)


def responder(status_code, headers=None):
    def handler(request):
        return httpx.Response(status_code, headers=headers or {})

    return httpx.MockTransport(handler)


def raiser(exception):
    def handler(request):
        raise exception

    return httpx.MockTransport(handler)


def test_successful_check_records_a_passing_result(monitor):
    result = execute_monitor(monitor, transport=responder(200))

    assert result.is_success
    assert result.status_code == 200
    assert result.error_type is None
    assert result.error_message is None
    assert result.response_time_ms is not None
    assert CheckResult.objects.count() == 1


def test_request_is_pinned_to_the_validated_ip(monitor):
    seen = {}

    def handler(request):
        seen['host'] = request.url.host
        seen['header'] = request.headers['host']
        return httpx.Response(200)

    execute_monitor(monitor, transport=httpx.MockTransport(handler))

    # Connect to the address we validated, but still address the real host so
    # the origin routes correctly and the certificate verifies.
    assert seen['host'] == PUBLIC_IP
    assert seen['header'] == 'example.com'


@pytest.mark.parametrize('redirect_status', [301, 302, 307, 308])
def test_redirects_are_followed_to_the_final_response(monitor, redirect_status):
    def handler(request):
        if request.url.path == '/status':
            return httpx.Response(
                redirect_status, headers={'location': 'https://example.com/final'}
            )
        return httpx.Response(200)

    result = execute_monitor(monitor, transport=httpx.MockTransport(handler))

    assert result.is_success
    assert result.status_code == 200
    assert CheckResult.objects.count() == 1


def test_every_redirect_hop_is_revalidated(monitor, monkeypatch):
    """A redirect into the private network is blocked at the hop that tries it."""
    calls = {'n': 0}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls['n'] += 1
        address = PUBLIC_IP if calls['n'] == 1 else '169.254.169.254'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)

    def handler(request):
        return httpx.Response(302, headers={'location': 'http://metadata.example.com/'})

    result = execute_monitor(monitor, transport=httpx.MockTransport(handler))

    assert not result.is_success
    assert result.error_type == ErrorType.BLOCKED_TARGET


def test_redirect_loop_is_cut_off(monitor):
    def handler(request):
        return httpx.Response(302, headers={'location': 'https://example.com/loop'})

    result = execute_monitor(monitor, transport=httpx.MockTransport(handler))

    assert not result.is_success
    assert result.error_type == ErrorType.TOO_MANY_REDIRECTS
    assert CheckResult.objects.count() == 1


@pytest.mark.parametrize('status_code', [400, 404, 500, 503])
def test_error_statuses_produce_a_failed_result(monitor, status_code):
    result = execute_monitor(monitor, transport=responder(status_code))

    assert not result.is_success
    assert result.status_code == status_code
    assert result.error_type == ErrorType.HTTP_ERROR
    assert str(status_code) in result.error_message
    assert CheckResult.objects.count() == 1


@pytest.mark.parametrize('status_code', [200, 204, 301, 399])
def test_statuses_below_400_count_as_reachable(monitor, status_code):
    result = execute_monitor(monitor, transport=responder(status_code))

    assert result.is_success
    assert result.error_type is None


def test_timeout_is_classified(monitor):
    result = execute_monitor(monitor, transport=raiser(httpx.ConnectTimeout('timed out')))

    assert not result.is_success
    assert result.error_type == ErrorType.TIMEOUT
    assert CheckResult.objects.count() == 1


def test_connection_failure_is_classified(monitor):
    result = execute_monitor(monitor, transport=raiser(httpx.ConnectError('connection refused')))

    assert not result.is_success
    assert result.error_type == ErrorType.CONNECTION_ERROR


def test_dns_failure_is_classified(monitor, monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror('Name or service not known')

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fail)

    result = execute_monitor(monitor, transport=responder(200))

    assert not result.is_success
    assert result.error_type == ErrorType.DNS_ERROR
    assert CheckResult.objects.count() == 1


def test_tls_failure_is_classified(monitor):
    def handler(request):
        raise httpx.ConnectError('handshake failed') from ssl.SSLCertVerificationError(
            'certificate verify failed: certificate has expired'
        )

    result = execute_monitor(monitor, transport=httpx.MockTransport(handler))

    assert not result.is_success
    assert result.error_type == ErrorType.TLS_ERROR


def test_blocked_target_produces_a_result_rather_than_an_exception(monitor, monkeypatch):
    def private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', private)

    result = execute_monitor(monitor, transport=responder(200))

    assert not result.is_success
    assert result.error_type == ErrorType.BLOCKED_TARGET
    assert CheckResult.objects.count() == 1


def test_an_unexpected_error_still_records_a_result(monitor, monkeypatch):
    """Nothing a target does may take the runner down."""
    import monitors.execution as execution

    def explode(*args, **kwargs):
        raise RuntimeError('something nobody anticipated')

    monkeypatch.setattr(execution, 'run_check', explode)

    result = execute_monitor(monitor)

    assert not result.is_success
    assert result.error_type == ErrorType.UNKNOWN_ERROR
    assert CheckResult.objects.count() == 1


def test_error_messages_stay_short(monitor):
    result = execute_monitor(monitor, transport=raiser(httpx.ConnectError('x' * 5000)))

    assert len(result.error_message) <= 300


@pytest.mark.parametrize(
    'transport',
    [responder(200), responder(500), raiser(httpx.ConnectError('nope'))],
    ids=['success', 'http-error', 'transport-error'],
)
def test_each_execution_persists_exactly_one_result(monitor, transport):
    execute_monitor(monitor, transport=transport)

    assert CheckResult.objects.filter(monitor=monitor).count() == 1


class TestSslMetadata:
    def test_https_check_records_certificate_expiry(self, monitor, monkeypatch):
        expires_at = timezone.now() + timedelta(days=30, hours=1)
        monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: expires_at)

        result = execute_monitor(monitor, transport=responder(200))

        assert result.ssl_expires_at == expires_at
        assert result.ssl_days_remaining == 30

    def test_expired_certificate_reports_negative_days(self, monitor, monkeypatch):
        # An hour of slack keeps this off the whole-day boundary: the clock the
        # check reads is always a little later than this one, and days are
        # floored, so a cert 3d1h expired reads as -4 rather than flipping
        # between -3 and -4 with timer granularity.
        expires_at = timezone.now() - timedelta(days=3, hours=1)
        monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: expires_at)

        result = execute_monitor(monitor, transport=responder(200))

        assert result.ssl_days_remaining == -4

    def test_plain_http_target_has_no_ssl_metadata(self, website, monitor, monkeypatch):
        website.url = 'http://example.com/status'
        website.save(update_fields=['url'])
        monitor.refresh_from_db()

        def unexpected(**kwargs):
            raise AssertionError('http targets must not be inspected for certificates')

        monkeypatch.setattr(tls, 'inspect_certificate', unexpected)

        result = execute_monitor(monitor, transport=responder(200))

        assert result.is_success
        assert result.ssl_expires_at is None
        assert result.ssl_days_remaining is None

    def test_unreadable_certificate_does_not_fail_the_check(self, monitor, monkeypatch):
        """Availability is decided by the request, not by our metadata probe."""
        monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)

        result = execute_monitor(monitor, transport=responder(200))

        assert result.is_success
        assert result.error_type is None
        assert result.ssl_expires_at is None
        assert result.ssl_days_remaining is None


class TestDaysRemaining:
    def test_counts_whole_days_forward(self):
        now = timezone.now()

        assert tls.days_remaining(now + timedelta(days=45, hours=2), now) == 45

    def test_is_negative_once_expired(self):
        now = timezone.now()

        assert tls.days_remaining(now - timedelta(days=1, hours=1), now) == -2

    def test_parses_openssl_not_after_format(self):
        parsed = tls.parse_not_after('Jun  1 12:00:00 2027 GMT')

        assert (parsed.year, parsed.month, parsed.day) == (2027, 6, 1)
