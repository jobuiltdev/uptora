"""Flow monitor configuration through the API, and through the execution pipeline.

The browser is faked here; what is under test is that a FLOW monitor can be
configured, that the configuration cannot be turned into an arbitrary request
sender, and that its results reach the incident engine like any other check.
"""

import socket
import time

import httpx
import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from incidents.models import Incident, IncidentStatus
from monitors import ssrf
from monitors.checks import tls
from monitors.checks.browser import Diagnostics, Navigation
from monitors.execution import execute_monitor
from monitors.models import (
    CheckResult,
    ErrorType,
    FlowConfig,
    FlowField,
    FlowFieldType,
    FlowKind,
    Monitor,
    MonitorType,
)

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


def flow_payload(website, **overrides):
    config = {
        'flow_kind': FlowKind.CONTACT_FORM,
        'submit_selector': 'button[type=submit]',
        'success_text': 'Thanks',
        'fields': [
            {'selector': '#name', 'field_type': 'TEXT', 'value': 'Uptora Monitor'},
            {'selector': '#email', 'field_type': 'EMAIL', 'value': 'monitor@example.com'},
        ],
    }
    config.update(overrides.pop('flow_config', {}))
    data = {
        'website': website.pk,
        'monitor_type': MonitorType.FLOW,
        'flow_config': config,
    }
    data.update(overrides)
    return data


def make_flow_monitor(website, **config_overrides):
    # A one-second budget: the failure paths spend the whole of it waiting for
    # a success state that never arrives, and ten seconds each would dominate
    # the suite for no extra coverage.
    monitor = Monitor.objects.create(
        website=website, monitor_type=MonitorType.FLOW, timeout_seconds=1
    )
    defaults = {
        'flow_kind': FlowKind.CONTACT_FORM,
        'submit_selector': 'button[type=submit]',
        'success_text': 'Thanks',
    }
    defaults.update(config_overrides)
    config = FlowConfig.objects.create(monitor=monitor, **defaults)
    FlowField.objects.create(
        flow_config=config, selector='#name', field_type=FlowFieldType.TEXT, value='Uptora'
    )
    return monitor


class StubSession:
    """A page that behaves like a contact form."""

    def __init__(self, status=200, succeed=True):
        self.status = status
        self.succeed = succeed
        self.text = 'Contact us'
        self.selectors = ['#name', '#email', 'button[type=submit]']
        self.diagnostics = Diagnostics()
        self.url = 'https://example.com/status'
        self.submission_events = []

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

    def current_url(self):
        return self.url

    def screenshot(self):
        return b'PNG'

    def sleep(self, milliseconds):
        # Yield for real, so a polling assertion idles rather than spinning.
        time.sleep(milliseconds / 1000)

    def fill(self, selector, value, timeout_ms):
        pass

    def set_checkbox(self, selector, checked, timeout_ms):
        pass

    def select_option(self, selector, value, timeout_ms):
        pass

    def begin_submission(self, policy=None):
        self.submission_events = []

    def submission_observed(self):
        return bool(self.submission_events)

    def submission_refused(self, outcome):
        return None

    def click(self, selector, timeout_ms):
        self.submission_events.append({'url': self.url, 'outcome': 'sent', 'reason': None})
        if self.succeed:
            self.text = 'Thanks for your message'


def session_factory(**kwargs):
    def make(timeout_ms, launch_args=()):
        return StubSession(**kwargs)

    return make


class TestFlowConfiguration:
    def test_a_contact_form_monitor_can_be_created(self, auth_client, website):
        response = auth_client.post(reverse(LIST_URL), flow_payload(website), format='json')

        assert response.status_code == 201, response.json()
        monitor = Monitor.objects.get(pk=response.json()['id'])
        assert monitor.monitor_type == MonitorType.FLOW
        assert monitor.flow_config.flow_kind == FlowKind.CONTACT_FORM
        assert monitor.flow_config.fields.count() == 2

    def test_the_configuration_is_returned(self, auth_client, website):
        body = auth_client.post(reverse(LIST_URL), flow_payload(website), format='json').json()

        config = body['flow_config']
        assert config['submit_selector'] == 'button[type=submit]'
        assert config['success_text'] == 'Thanks'
        assert [field['selector'] for field in config['fields']] == ['#name', '#email']

    def test_fields_keep_their_configured_order(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['fields'] = [
            {'selector': '#message', 'field_type': 'TEXTAREA', 'value': 'c', 'position': 2},
            {'selector': '#name', 'field_type': 'TEXT', 'value': 'a', 'position': 0},
            {'selector': '#email', 'field_type': 'EMAIL', 'value': 'b', 'position': 1},
        ]

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        monitor = Monitor.objects.get(pk=response.json()['id'])
        assert [f.selector for f in monitor.flow_config.fields.all()] == [
            '#name',
            '#email',
            '#message',
        ]

    def test_the_configuration_can_be_updated(self, auth_client, website):
        created = auth_client.post(reverse(LIST_URL), flow_payload(website), format='json').json()

        response = auth_client.patch(
            reverse(DETAIL_URL, args=[created['id']]),
            {
                'flow_config': {
                    'flow_kind': FlowKind.CONTACT_FORM,
                    'submit_selector': '#send',
                    'success_selector': '.done',
                    'fields': [{'selector': '#full-name', 'field_type': 'TEXT', 'value': 'x'}],
                }
            },
            format='json',
        )

        assert response.status_code == 200, response.json()
        config = FlowConfig.objects.get(monitor_id=created['id'])
        assert config.submit_selector == '#send'
        assert config.success_selector == '.done'
        # Replaced wholesale, so the old fields do not linger.
        assert [f.selector for f in config.fields.all()] == ['#full-name']
        assert FlowField.objects.count() == 1

    def test_a_flow_monitor_without_configuration_is_rejected(self, auth_client, website):
        response = auth_client.post(
            reverse(LIST_URL),
            {'website': website.pk, 'monitor_type': MonitorType.FLOW},
            format='json',
        )

        assert response.status_code == 400
        assert 'flow_config' in response.json()
        assert not Monitor.objects.exists()

    def test_a_configuration_without_a_success_assertion_is_rejected(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config'].pop('success_text')

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 400
        assert 'flow_config' in response.json()
        assert not Monitor.objects.exists()

    def test_blank_success_values_do_not_count_as_configured(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['success_text'] = '   '
        payload['flow_config']['success_selector'] = ''

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 400

    @pytest.mark.parametrize('assertion', ['success_selector', 'success_url_contains'])
    def test_any_single_assertion_is_enough(self, auth_client, website, assertion):
        payload = flow_payload(website)
        payload['flow_config'].pop('success_text')
        payload['flow_config'][assertion] = '/thanks'

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 201, response.json()

    @pytest.mark.parametrize('flow_kind', ['LOGIN', 'CHECKOUT', 'SEARCH', 'contact_form'])
    def test_unsupported_flow_kinds_are_rejected(self, auth_client, website, flow_kind):
        payload = flow_payload(website)
        payload['flow_config']['flow_kind'] = flow_kind

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 400
        assert not Monitor.objects.exists()

    def test_a_missing_submit_selector_is_rejected(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['submit_selector'] = '   '

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 400

    def test_a_blank_field_selector_is_rejected(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['fields'] = [{'selector': '', 'value': 'x'}]

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 400

    def test_another_users_website_is_still_rejected(self, auth_client, other_website):
        response = auth_client.post(reverse(LIST_URL), flow_payload(other_website), format='json')

        assert response.status_code == 400
        assert not Monitor.objects.exists()
        assert not FlowConfig.objects.exists()

    def test_a_rejected_monitor_leaves_no_configuration_behind(self, auth_client, other_website):
        """Creation is transactional: no orphaned FlowConfig on failure."""
        auth_client.post(reverse(LIST_URL), flow_payload(other_website), format='json')

        assert not FlowConfig.objects.exists()
        assert not FlowField.objects.exists()


class TestAntiAbuseBoundary:
    """The configuration must not become a programmable request sender."""

    def test_no_submission_url_can_be_configured(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['action'] = 'https://victim.example/spam'
        payload['flow_config']['url'] = 'https://victim.example/spam'
        payload['flow_config']['target_url'] = 'https://victim.example/spam'

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 201
        config = FlowConfig.objects.get()
        # Unknown keys are ignored, and no such column exists to hold them.
        assert not hasattr(config, 'action')
        assert not hasattr(config, 'url')
        assert not hasattr(config, 'target_url')

    def test_the_flow_always_runs_against_the_website_url(self, website):
        """The only destination is the monitored site's own URL."""
        from monitors.checks.flow import build_plan

        monitor = make_flow_monitor(website)
        plan = build_plan(monitor)

        assert not hasattr(plan, 'url')
        assert not hasattr(plan, 'action')

    def test_no_script_can_be_configured(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['script'] = 'fetch("http://169.254.169.254/")'
        payload['flow_config']['fields'] = [
            {
                'selector': '#name',
                'field_type': 'TEXT',
                'value': 'x',
                'script': 'alert(1)',
            }
        ]

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 201
        config = FlowConfig.objects.get()
        assert not hasattr(config, 'script')
        assert not hasattr(config.fields.first(), 'script')

    def test_field_types_are_a_closed_set(self, auth_client, website):
        payload = flow_payload(website)
        payload['flow_config']['fields'] = [
            {'selector': '#name', 'field_type': 'EVALUATE', 'value': 'alert(1)'}
        ]

        response = auth_client.post(reverse(LIST_URL), payload, format='json')

        assert response.status_code == 400

    def test_running_a_flow_stays_throttled(self, auth_client, website, monkeypatch):
        """The manual run limit from Milestone 1.5 still applies to writes.

        Execution is stubbed so the twelve requests land inside one rate
        window; a real browser per run would take longer than the window and
        the limit would never be reached.
        """
        import monitors.views as views

        monitor = make_flow_monitor(website)
        monkeypatch.setattr(
            views,
            'execute_monitor',
            lambda m: execute_monitor(m, session_factory=session_factory()),
        )

        statuses = [
            auth_client.post(reverse(RUN_URL, args=[monitor.pk])).status_code for _ in range(12)
        ]

        assert statuses.count(201) == 10
        assert statuses[-1] == 429


class TestOtherMonitorTypesUnchanged:
    def test_an_http_monitor_may_not_carry_flow_config(self, auth_client, website):
        response = auth_client.post(
            reverse(LIST_URL),
            {
                'website': website.pk,
                'monitor_type': MonitorType.HTTP,
                'flow_config': {
                    'flow_kind': FlowKind.CONTACT_FORM,
                    'submit_selector': '#go',
                    'success_text': 'Thanks',
                    'fields': [],
                },
            },
            format='json',
        )

        assert response.status_code == 400
        assert 'flow_config' in response.json()

    def test_a_browser_monitor_may_not_carry_flow_config(self, auth_client, website):
        response = auth_client.post(
            reverse(LIST_URL),
            {
                'website': website.pk,
                'monitor_type': MonitorType.BROWSER,
                'flow_config': {
                    'flow_kind': FlowKind.CONTACT_FORM,
                    'submit_selector': '#go',
                    'success_text': 'Thanks',
                    'fields': [],
                },
            },
            format='json',
        )

        assert response.status_code == 400

    def test_http_monitors_still_work(self, monitor):
        result = execute_monitor(
            monitor, transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )

        assert result.is_success
        assert result.error_type is None

    def test_an_http_monitor_reports_no_flow_config(self, auth_client, monitor):
        body = auth_client.get(reverse(DETAIL_URL, args=[monitor.pk])).json()

        assert body['flow_config'] is None


class TestDatabaseInvariants:
    def test_the_database_refuses_a_config_with_no_assertion(self, website):
        monitor = Monitor.objects.create(website=website, monitor_type=MonitorType.FLOW)

        with pytest.raises(IntegrityError), transaction.atomic():
            FlowConfig.objects.create(
                monitor=monitor,
                flow_kind=FlowKind.CONTACT_FORM,
                submit_selector='#go',
            )

    def test_deleting_a_monitor_removes_its_configuration(self, website):
        monitor = make_flow_monitor(website)

        monitor.delete()

        assert not FlowConfig.objects.exists()
        assert not FlowField.objects.exists()


class TestExecution:
    def test_a_flow_monitor_runs_through_the_generic_pipeline(self, website):
        monitor = make_flow_monitor(website)

        result = execute_monitor(monitor, session_factory=session_factory())

        assert result.is_success, result.error_message
        assert CheckResult.objects.count() == 1
        result.refresh_from_db()
        assert result.incident_processed_at is not None

    def test_a_failing_flow_records_a_flow_error(self, website):
        monitor = make_flow_monitor(website)

        result = execute_monitor(monitor, session_factory=session_factory(succeed=False))

        assert not result.is_success
        assert result.error_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING
        assert result.screenshot

    def test_a_flow_monitor_without_configuration_fails_precisely(self, website):
        """Not BROWSER_ERROR, not UNKNOWN_ERROR: the configuration is the fault."""
        monitor = Monitor.objects.create(website=website, monitor_type=MonitorType.FLOW)

        result = execute_monitor(monitor, session_factory=session_factory())

        assert result.error_type == ErrorType.FLOW_CONFIGURATION_ERROR
        assert CheckResult.objects.count() == 1

    def test_exactly_one_result_per_execution(self, website):
        monitor = make_flow_monitor(website)

        execute_monitor(monitor, session_factory=session_factory(succeed=False))

        assert CheckResult.objects.filter(monitor=monitor).count() == 1

    def test_the_run_endpoint_executes_a_flow(self, auth_client, website, monkeypatch):
        import monitors.views as views

        monitor = make_flow_monitor(website)
        monkeypatch.setattr(
            views,
            'execute_monitor',
            lambda m: execute_monitor(m, session_factory=session_factory()),
        )

        response = auth_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 201
        assert response.json()['is_success'] is True

    def test_another_user_cannot_run_a_flow(self, other_client, website):
        monitor = make_flow_monitor(website)

        response = other_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 404
        assert not CheckResult.objects.exists()

    def test_another_user_cannot_read_the_configuration(self, other_client, website):
        monitor = make_flow_monitor(website)

        response = other_client.get(reverse(DETAIL_URL, args=[monitor.pk]))

        assert response.status_code == 404


class TestIncidentIntegration:
    def test_two_flow_failures_open_an_incident(self, website):
        monitor = make_flow_monitor(website)
        factory = session_factory(succeed=False)

        execute_monitor(monitor, session_factory=factory)
        assert not Incident.objects.exists()

        execute_monitor(monitor, session_factory=factory)

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.failure_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING
        assert incident.failure_count == 2

    def test_two_successes_resolve_it(self, website):
        monitor = make_flow_monitor(website)

        for _ in range(2):
            execute_monitor(monitor, session_factory=session_factory(succeed=False))
        execute_monitor(monitor, session_factory=session_factory())

        assert Incident.objects.get().status == IncidentStatus.OPEN

        execute_monitor(monitor, session_factory=session_factory())

        assert Incident.objects.get().status == IncidentStatus.RESOLVED

    def test_the_monitor_serializer_reports_the_open_incident(self, auth_client, website):
        monitor = make_flow_monitor(website)
        factory = session_factory(succeed=False)
        for _ in range(2):
            execute_monitor(monitor, session_factory=factory)

        body = auth_client.get(reverse(DETAIL_URL, args=[monitor.pk])).json()

        assert body['has_open_incident'] is True

    def test_incident_metadata_carries_no_configured_values(self, website):
        monitor = make_flow_monitor(website)
        config = monitor.flow_config
        config.fields.update(value='Priya Raman priya@example.com')
        factory = session_factory(succeed=False)

        for _ in range(2):
            execute_monitor(monitor, session_factory=factory)

        incident = Incident.objects.get()
        blob = ' '.join(
            filter(None, [incident.initial_error_message, incident.latest_error_message])
        )
        assert 'Priya' not in blob
        assert 'priya@example.com' not in blob
