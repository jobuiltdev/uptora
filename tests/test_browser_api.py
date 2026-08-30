"""Browser monitors through the API, and through the existing execution pipeline.

The browser itself is faked; what is under test is that a BROWSER monitor is
configurable, ownership-scoped, dispatched to the browser check, and that its
results flow into the incident engine exactly like an HTTP monitor's.
"""

import socket

import httpx
import pytest
from django.urls import reverse

from incidents.models import Incident, IncidentStatus
from monitors import ssrf
from monitors.checks import tls
from monitors.checks.browser import Diagnostics, Navigation
from monitors.execution import execute_monitor
from monitors.models import CheckResult, ErrorType, Monitor, MonitorType

pytestmark = pytest.mark.django_db

LIST_URL = 'monitor-list'
DETAIL_URL = 'monitor-detail'
RUN_URL = 'monitor-run'

PUBLIC_IP = '93.184.216.34'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)


class StubSession:
    def __init__(self, status=200, text='Uptora is up', selectors=('#ready',), screenshot=b'PNG'):
        self.status = status
        self.text = text
        self.selectors = selectors
        self._screenshot = screenshot
        self.diagnostics = Diagnostics()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def navigate(self, url, timeout_ms):
        return Navigation(status=self.status, final_url=url)

    def has_text(self, text):
        return text in self.text

    def wait_for_selector(self, selector, timeout_ms):
        return selector in self.selectors

    def screenshot(self):
        return self._screenshot


def session_factory(**kwargs):
    def make(timeout_ms, launch_args=()):
        return StubSession(**kwargs)

    return make


def payload(website, **overrides):
    data = {'website': website.pk, 'monitor_type': MonitorType.BROWSER}
    data.update(overrides)
    return data


class TestBrowserMonitorApi:
    def test_user_can_create_a_browser_monitor(self, auth_client, website):
        response = auth_client.post(reverse(LIST_URL), payload(website), format='json')

        assert response.status_code == 201
        monitor = Monitor.objects.get(pk=response.json()['id'])
        assert monitor.monitor_type == MonitorType.BROWSER

    def test_expectation_fields_are_accepted(self, auth_client, website):
        response = auth_client.post(
            reverse(LIST_URL),
            payload(website, expected_text='Uptora is up', expected_selector='#ready'),
            format='json',
        )

        assert response.status_code == 201
        body = response.json()
        assert body['expected_text'] == 'Uptora is up'
        assert body['expected_selector'] == '#ready'

    def test_expectations_are_optional(self, auth_client, website):
        response = auth_client.post(reverse(LIST_URL), payload(website), format='json')

        body = response.json()
        assert body['expected_text'] is None
        assert body['expected_selector'] is None

    def test_expectations_can_be_updated(self, auth_client, browser_monitor):
        response = auth_client.patch(
            reverse(DETAIL_URL, args=[browser_monitor.pk]),
            {'expected_selector': '#loaded'},
            format='json',
        )

        assert response.status_code == 200
        browser_monitor.refresh_from_db()
        assert browser_monitor.expected_selector == '#loaded'

    @pytest.mark.parametrize('monitor_type', ['FLOW', 'PING', 'browser', 'CHROME'])
    def test_unknown_monitor_types_are_still_rejected(self, auth_client, website, monitor_type):
        response = auth_client.post(
            reverse(LIST_URL), payload(website, monitor_type=monitor_type), format='json'
        )

        assert response.status_code == 400
        assert 'monitor_type' in response.json()

    def test_http_remains_the_default(self, auth_client, website):
        response = auth_client.post(reverse(LIST_URL), {'website': website.pk}, format='json')

        assert response.status_code == 201
        assert response.json()['monitor_type'] == MonitorType.HTTP

    def test_a_browser_monitor_cannot_target_another_users_website(
        self, auth_client, other_website
    ):
        response = auth_client.post(reverse(LIST_URL), payload(other_website), format='json')

        assert response.status_code == 400
        assert not Monitor.objects.exists()

    def test_another_users_browser_monitor_is_not_found(self, auth_client, other_website):
        foreign = Monitor.objects.create(website=other_website, monitor_type=MonitorType.BROWSER)

        assert auth_client.get(reverse(DETAIL_URL, args=[foreign.pk])).status_code == 404


class TestHttpUnchanged:
    def test_http_monitors_ignore_browser_expectations(self, auth_client, website):
        """Set but never read: an HTTP check does not consult these fields."""
        monitor = Monitor.objects.create(
            website=website,
            monitor_type=MonitorType.HTTP,
            expected_text='never checked',
            expected_selector='#never-checked',
        )

        result = execute_monitor(
            monitor, transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )

        assert result.is_success
        assert result.error_type is None

    def test_http_results_now_record_the_final_url(self, auth_client, monitor):
        result = execute_monitor(
            monitor, transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )

        assert result.final_url == monitor.website.url

    def test_http_results_never_carry_a_screenshot(self, monitor):
        result = execute_monitor(
            monitor, transport=httpx.MockTransport(lambda request: httpx.Response(500))
        )

        assert not result.is_success
        assert not result.screenshot


class TestManualRun:
    def test_run_endpoint_executes_a_browser_monitor(
        self, auth_client, browser_monitor, monkeypatch
    ):
        import monitors.views as views

        monkeypatch.setattr(
            views,
            'execute_monitor',
            lambda m: execute_monitor(m, session_factory=session_factory()),
        )

        response = auth_client.post(reverse(RUN_URL, args=[browser_monitor.pk]))

        assert response.status_code == 201
        body = response.json()
        assert body['is_success'] is True
        assert body['status_code'] == 200
        assert CheckResult.objects.count() == 1

    def test_a_failing_browser_monitor_returns_a_result(
        self, auth_client, browser_monitor, monkeypatch
    ):
        import monitors.views as views

        monkeypatch.setattr(
            views,
            'execute_monitor',
            lambda m: execute_monitor(m, session_factory=session_factory(status=503)),
        )

        response = auth_client.post(reverse(RUN_URL, args=[browser_monitor.pk]))

        assert response.status_code == 201
        assert response.json()['error_type'] == ErrorType.HTTP_ERROR
        assert response.json()['screenshot'] is not None

    def test_a_foreign_browser_monitor_cannot_be_run(self, other_client, browser_monitor):
        response = other_client.post(reverse(RUN_URL, args=[browser_monitor.pk]))

        assert response.status_code == 404
        assert not CheckResult.objects.exists()


class TestScreenshotPersistence:
    def test_a_failed_browser_check_stores_a_screenshot(self, browser_monitor):
        result = execute_monitor(
            browser_monitor, session_factory=session_factory(status=500, screenshot=b'PNGDATA')
        )

        assert not result.is_success
        assert result.screenshot
        assert result.screenshot.read() == b'PNGDATA'

    def test_the_stored_name_is_not_guessable(self, browser_monitor):
        result = execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        name = result.screenshot.name
        basename = name.rsplit('/', 1)[-1]
        assert name.startswith('check-screenshots/')
        assert basename.endswith('.png')
        # A 24-byte urlsafe token, so the name carries no sequence to walk.
        assert len(basename.removesuffix('.png')) >= 32

    def test_two_failures_do_not_collide(self, browser_monitor):
        first = execute_monitor(browser_monitor, session_factory=session_factory(status=500))
        second = execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        assert first.screenshot.name != second.screenshot.name

    def test_a_successful_browser_check_stores_nothing(self, browser_monitor):
        result = execute_monitor(browser_monitor, session_factory=session_factory())

        assert result.is_success
        assert not result.screenshot

    def test_a_storage_failure_leaves_the_observation_intact(self, browser_monitor, monkeypatch):
        """Evidence is best effort; the finding is not."""
        from django.db.models.fields.files import FieldFile

        def explode(self, name, content, save=True):
            raise OSError('media volume is full')

        monkeypatch.setattr(FieldFile, 'save', explode)

        result = execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        assert not result.is_success
        assert result.error_type == ErrorType.HTTP_ERROR
        assert CheckResult.objects.count() == 1


class TestIncidentIntegration:
    def test_a_browser_result_reaches_the_incident_engine(self, browser_monitor):
        result = execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        result.refresh_from_db()
        assert result.incident_processed_at is not None

    def test_two_browser_failures_open_an_incident(self, browser_monitor):
        execute_monitor(browser_monitor, session_factory=session_factory(status=500))
        assert not Incident.objects.exists()

        execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.failure_type == ErrorType.HTTP_ERROR
        assert incident.failure_count == 2

    def test_two_recoveries_resolve_it(self, browser_monitor):
        for _ in range(2):
            execute_monitor(browser_monitor, session_factory=session_factory(status=500))
        execute_monitor(browser_monitor, session_factory=session_factory())

        assert Incident.objects.get().status == IncidentStatus.OPEN

        execute_monitor(browser_monitor, session_factory=session_factory())

        assert Incident.objects.get().status == IncidentStatus.RESOLVED

    def test_an_expectation_failure_opens_an_incident_too(self, website):
        monitor = Monitor.objects.create(
            website=website,
            monitor_type=MonitorType.BROWSER,
            expected_text='Uptora is up',
        )
        factory = session_factory(text='Maintenance in progress')

        for _ in range(2):
            execute_monitor(monitor, session_factory=factory)

        incident = Incident.objects.get()
        assert incident.failure_type == ErrorType.EXPECTED_TEXT_MISSING

    def test_each_browser_execution_persists_exactly_one_result(self, browser_monitor):
        execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        assert CheckResult.objects.filter(monitor=browser_monitor).count() == 1

    def test_the_monitor_serializer_reports_the_open_incident(self, auth_client, browser_monitor):
        for _ in range(2):
            execute_monitor(browser_monitor, session_factory=session_factory(status=500))

        body = auth_client.get(reverse(DETAIL_URL, args=[browser_monitor.pk])).json()

        assert body['has_open_incident'] is True
