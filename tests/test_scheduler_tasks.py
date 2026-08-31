"""The Celery layer: routing, message contents, concurrency guards and logging.

No broker is contacted. apply_async is stubbed everywhere, so these tests assert
what *would* be sent rather than sending it.
"""

import logging
import socket
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from monitors import ssrf, tasks
from monitors.checks import tls
from monitors.checks.browser import Diagnostics, Navigation
from monitors.models import (
    FlowConfig,
    FlowField,
    FlowFieldType,
    FlowKind,
    Monitor,
    MonitorRun,
    MonitorType,
    RunStatus,
)
from monitors.scheduling import BROWSER_QUEUE, HTTP_QUEUE, claim_due_monitors, execute_run

pytestmark = pytest.mark.django_db

PUBLIC_IP = '93.184.216.34'
SENSITIVE_VALUE = 'Priya Raman priya@example.com'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been put on the broker."""
    messages = []

    def fake_apply_async(args=None, queue=None, **kwargs):
        messages.append({'args': args, 'queue': queue, 'kwargs': kwargs})

    monkeypatch.setattr(tasks.execute_monitor_run, 'apply_async', fake_apply_async)
    return messages


def due_monitor(website, **overrides):
    monitor = Monitor.objects.create(website=website, **overrides)
    Monitor.objects.filter(pk=monitor.pk).update(
        next_check_at=timezone.now() - timedelta(minutes=1)
    )
    monitor.refresh_from_db()
    return monitor


class TestDispatcherTask:
    def test_it_reports_how_many_it_claimed(self, website, sent):
        for _ in range(3):
            due_monitor(website)

        assert tasks.dispatch_due_monitors() == {'dispatched': 3, 'recovered': 0}

    def test_it_enqueues_one_message_per_claimed_run(self, website, sent):
        due_monitor(website)

        tasks.dispatch_due_monitors()

        assert len(sent) == 1
        assert sent[0]['queue'] == HTTP_QUEUE

    def test_browser_work_goes_to_the_browser_queue(self, website, sent):
        due_monitor(website, monitor_type=MonitorType.BROWSER)

        tasks.dispatch_due_monitors()

        assert sent[0]['queue'] == BROWSER_QUEUE

    def test_flow_work_goes_to_the_browser_queue(self, website, sent):
        due_monitor(website, monitor_type=MonitorType.FLOW)

        tasks.dispatch_due_monitors()

        assert sent[0]['queue'] == BROWSER_QUEUE

    def test_nothing_due_enqueues_nothing(self, monitor, sent):
        Monitor.objects.filter(pk=monitor.pk).update(
            next_check_at=timezone.now() + timedelta(hours=1)
        )

        assert tasks.dispatch_due_monitors() == {'dispatched': 0, 'recovered': 0}
        assert sent == []

    def test_mixed_types_are_routed_separately(self, website, sent):
        due_monitor(website, monitor_type=MonitorType.HTTP)
        due_monitor(website, monitor_type=MonitorType.BROWSER)

        tasks.dispatch_due_monitors()

        assert sorted(message['queue'] for message in sent) == [BROWSER_QUEUE, HTTP_QUEUE]


class TestMessageContents:
    """Broker payloads are readable by anyone with Redis. They carry ids only."""

    def test_the_message_carries_only_a_run_id(self, website, sent):
        due_monitor(website)

        tasks.dispatch_due_monitors()

        assert len(sent[0]['args']) == 1
        run = MonitorRun.objects.get()
        assert sent[0]['args'] == [str(run.id)]

    def test_the_message_is_a_plain_string(self, website, sent):
        """Serializable without pickling a model instance."""
        due_monitor(website)

        tasks.dispatch_due_monitors()

        assert isinstance(sent[0]['args'][0], str)

    def test_no_configured_flow_value_reaches_the_broker(self, website, sent):
        monitor = due_monitor(website, monitor_type=MonitorType.FLOW)
        config = FlowConfig.objects.create(
            monitor=monitor,
            flow_kind=FlowKind.CONTACT_FORM,
            submit_selector='#send',
            success_text='Thanks',
        )
        FlowField.objects.create(
            flow_config=config,
            selector='#name',
            field_type=FlowFieldType.TEXT,
            value=SENSITIVE_VALUE,
        )

        tasks.dispatch_due_monitors()

        payload = repr(sent)
        assert SENSITIVE_VALUE not in payload
        assert 'priya@example.com' not in payload
        assert '#name' not in payload
        assert monitor.website.url not in payload


class TestSlotUniqueness:
    def test_the_database_refuses_two_runs_for_one_slot(self, website):
        monitor = due_monitor(website)
        slot = monitor.next_check_at
        MonitorRun.objects.create(monitor=monitor, scheduled_for=slot)

        with pytest.raises(IntegrityError), transaction.atomic():
            MonitorRun.objects.create(monitor=monitor, scheduled_for=slot)

    def test_a_dispatcher_losing_the_race_enqueues_nothing(self, website, sent):
        """Another dispatcher already created the run for this slot."""
        monitor = due_monitor(website)
        MonitorRun.objects.create(monitor=monitor, scheduled_for=monitor.next_check_at)

        claimed = claim_due_monitors()

        assert claimed == []
        assert MonitorRun.objects.count() == 1

    def test_the_loser_still_advances_the_schedule(self, website):
        monitor = due_monitor(website)
        MonitorRun.objects.create(monitor=monitor, scheduled_for=monitor.next_check_at)

        claim_due_monitors()

        monitor.refresh_from_db()
        assert monitor.next_check_at > timezone.now()

    def test_different_slots_for_one_monitor_are_fine(self, website):
        monitor = due_monitor(website)
        now = timezone.now()
        MonitorRun.objects.create(monitor=monitor, scheduled_for=now)
        MonitorRun.objects.create(monitor=monitor, scheduled_for=now + timedelta(minutes=1))

        assert MonitorRun.objects.count() == 2


class TestExecutionTaskWrapper:
    def test_it_returns_the_final_status(self, website, sent, monkeypatch):
        import monitors.tasks as task_module

        due_monitor(website)
        tasks.dispatch_due_monitors()
        run_id = MonitorRun.objects.get().id

        monkeypatch.setattr(task_module, 'execute_run', lambda rid: MonitorRun.objects.get(pk=rid))

        assert tasks.execute_monitor_run(str(run_id)) == RunStatus.PENDING

    def test_exhausted_retries_mark_the_run_internally_failed(self, website, sent, monkeypatch):
        import monitors.tasks as task_module

        due_monitor(website)
        tasks.dispatch_due_monitors()
        run_id = MonitorRun.objects.get().id

        def explode(rid):
            raise RuntimeError('broker unreachable')

        monkeypatch.setattr(task_module, 'execute_run', explode)

        # Celery's own hook for standing in a request context: this is a task
        # whose retries are already spent.
        tasks.execute_monitor_run.push_request(retries=99)
        try:
            result = tasks.execute_monitor_run(str(run_id))
        finally:
            tasks.execute_monitor_run.pop_request()

        assert result == 'FAILED_INTERNAL'
        run = MonitorRun.objects.get(pk=run_id)
        assert run.status == RunStatus.FAILED_INTERNAL
        assert 'broker unreachable' in run.internal_error


class TestLogging:
    def test_scheduler_logs_carry_no_configured_values(self, website, caplog):
        monitor = due_monitor(website, monitor_type=MonitorType.FLOW)
        config = FlowConfig.objects.create(
            monitor=monitor,
            flow_kind=FlowKind.CONTACT_FORM,
            submit_selector='#send',
            success_text='Thanks',
        )
        FlowField.objects.create(
            flow_config=config,
            selector='#name',
            field_type=FlowFieldType.TEXT,
            value=SENSITIVE_VALUE,
        )
        ((run, _),) = claim_due_monitors()

        class StubSession:
            def __init__(self):
                self.diagnostics = Diagnostics()
                self.submission_events = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def navigate(self, url, timeout_ms):
                return Navigation(status=200, final_url=url)

            def has_text(self, text):
                return text == 'Thanks'

            def wait_for_selector(self, selector, timeout_ms):
                return True

            def current_url(self):
                return 'https://example.com/status'

            def screenshot(self):
                return None

            def sleep(self, milliseconds):
                pass

            def fill(self, selector, value, timeout_ms):
                pass

            def begin_submission(self, policy=None):
                self.submission_events = []

            def submission_observed(self):
                return True

            def submission_refused(self, outcome):
                return None

            def click(self, selector, timeout_ms):
                self.submission_events.append({'url': 'x', 'outcome': 'sent'})

        with caplog.at_level(logging.DEBUG):
            execute_run(run.id, session_factory=lambda t, a=(): StubSession())

        text = caplog.text
        assert SENSITIVE_VALUE not in text
        assert 'priya@example.com' not in text

    def test_the_dispatcher_logs_a_count(self, website, sent, caplog):
        due_monitor(website)

        with caplog.at_level(logging.INFO, logger='monitors.scheduling'):
            tasks.dispatch_due_monitors()

        assert 'claimed 1 monitor' in caplog.text

    def test_a_run_logs_its_identifiers(self, website, sent, caplog):
        import httpx

        due_monitor(website)
        tasks.dispatch_due_monitors()
        run = MonitorRun.objects.get()

        with caplog.at_level(logging.INFO, logger='monitors.scheduling'):
            execute_run(
                run.id,
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )

        assert str(run.id) in caplog.text
        assert 'completed' in caplog.text
