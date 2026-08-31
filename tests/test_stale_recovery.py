"""Stale lease recovery.

A worker that dies holding a lease leaves its run RUNNING forever unless
something goes looking for it. These tests cover that scan, and -- more
importantly -- that a rescued run resumes from its durable state rather than
starting over.
"""

import socket
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from incidents.models import Incident
from monitors import ssrf, tasks
from monitors.checks import tls
from monitors.models import CheckResult, Monitor, MonitorRun, MonitorType, RunStatus
from monitors.scheduling import (
    BROWSER_QUEUE,
    HTTP_QUEUE,
    RECOVERY_DEBOUNCE,
    claim_due_monitors,
    claim_run,
    execute_run,
    mark_failed_internal,
    recover_stale_runs,
)

pytestmark = pytest.mark.django_db

PUBLIC_IP = '93.184.216.34'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)


class Network:
    def __init__(self, status_code=200):
        self.calls = 0
        self.status_code = status_code

    def transport(self):
        def handler(request):
            self.calls += 1
            return httpx.Response(self.status_code)

        return httpx.MockTransport(handler)


@pytest.fixture
def network():
    return Network()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(
        tasks.execute_monitor_run,
        'apply_async',
        lambda args=None, queue=None, **kwargs: messages.append({'args': args, 'queue': queue}),
    )
    return messages


def due_monitor(website, **overrides):
    monitor = Monitor.objects.create(website=website, **overrides)
    Monitor.objects.filter(pk=monitor.pk).update(
        next_check_at=timezone.now() - timedelta(minutes=1)
    )
    monitor.refresh_from_db()
    return monitor


@pytest.fixture
def pending_run(website):
    due_monitor(website)
    ((run, _),) = claim_due_monitors()
    return run


def abandon(run):
    """Simulate a worker that claimed the run and then died."""
    claim_run(run.id)
    MonitorRun.objects.filter(pk=run.id).update(
        lease_expires_at=timezone.now() - timedelta(minutes=5)
    )
    return MonitorRun.objects.get(pk=run.id)


class TestDiscovery:
    def test_an_expired_lease_is_discovered(self, pending_run):
        abandon(pending_run)

        recovered = recover_stale_runs()

        assert [run.id for run, _ in recovered] == [pending_run.id]

    def test_a_live_lease_is_ignored(self, pending_run):
        claim_run(pending_run.id)

        assert recover_stale_runs() == []

    def test_a_pending_run_is_not_stale(self, pending_run):
        """It was never claimed, so nothing abandoned it."""
        assert pending_run.status == RunStatus.PENDING

        assert recover_stale_runs() == []

    def test_a_completed_run_is_ignored(self, pending_run, network):
        execute_run(pending_run.id, transport=network.transport())

        assert recover_stale_runs() == []

    def test_a_cancelled_run_is_ignored(self, pending_run, network):
        Monitor.objects.filter(pk=pending_run.monitor_id).update(is_enabled=False)
        execute_run(pending_run.id, transport=network.transport())
        assert MonitorRun.objects.get(pk=pending_run.id).status == RunStatus.CANCELLED

        assert recover_stale_runs() == []

    def test_a_failed_internal_run_is_ignored(self, pending_run):
        """Terminal by current semantics; reviving it is a separate decision."""
        abandon(pending_run)
        mark_failed_internal(pending_run.id, 'broker unreachable')

        assert recover_stale_runs() == []

    def test_a_run_with_no_lease_recorded_is_ignored(self, pending_run):
        MonitorRun.objects.filter(pk=pending_run.id).update(
            status=RunStatus.RUNNING, lease_expires_at=None
        )

        assert recover_stale_runs() == []

    def test_the_batch_size_bounds_a_scan(self, website):
        for _ in range(4):
            due_monitor(website)
        for run, _ in claim_due_monitors():
            abandon(run)

        assert len(recover_stale_runs(limit=2)) == 2


class TestScanSideEffects:
    """Discovery observes; it must not change the work."""

    def test_no_second_run_is_created(self, pending_run):
        abandon(pending_run)

        recover_stale_runs()

        assert MonitorRun.objects.count() == 1

    def test_the_monitor_schedule_is_untouched(self, pending_run):
        abandon(pending_run)
        monitor = Monitor.objects.get(pk=pending_run.monitor_id)
        before = monitor.next_check_at

        recover_stale_runs()

        monitor.refresh_from_db()
        assert monitor.next_check_at == before

    def test_no_check_result_is_created(self, pending_run):
        abandon(pending_run)

        recover_stale_runs()

        assert not CheckResult.objects.exists()

    def test_an_existing_result_is_left_alone(self, pending_run, network, monkeypatch):
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'process_recorded_result',
            lambda rid: (_ for _ in ()).throw(RuntimeError('boom')),
        )
        with pytest.raises(RuntimeError):
            execute_run(pending_run.id, transport=network.transport())
        monkeypatch.undo()
        linked = MonitorRun.objects.get(pk=pending_run.id).check_result_id

        recover_stale_runs()

        assert MonitorRun.objects.get(pk=pending_run.id).check_result_id == linked
        assert CheckResult.objects.count() == 1

    def test_the_run_stays_running(self, pending_run):
        abandon(pending_run)

        recover_stale_runs()

        assert MonitorRun.objects.get(pk=pending_run.id).status == RunStatus.RUNNING


class TestAttemptCount:
    def test_discovery_alone_does_not_count_as_an_attempt(self, pending_run):
        abandon(pending_run)
        before = MonitorRun.objects.get(pk=pending_run.id).attempt_count

        recover_stale_runs()
        recover_stale_runs()

        assert MonitorRun.objects.get(pk=pending_run.id).attempt_count == before

    def test_a_worker_reclaiming_counts_as_an_attempt(self, pending_run, network):
        abandon(pending_run)
        before = MonitorRun.objects.get(pk=pending_run.id).attempt_count

        execute_run(pending_run.id, transport=network.transport())

        assert MonitorRun.objects.get(pk=pending_run.id).attempt_count == before + 1

    def test_repeated_crashes_are_visible(self, pending_run):
        for _ in range(3):
            abandon(pending_run)
            recover_stale_runs()

        assert MonitorRun.objects.get(pk=pending_run.id).attempt_count == 3


class TestResumeWithoutResult:
    """A. no observation was recorded, so the network may run."""

    def test_a_recovered_worker_runs_the_check(self, pending_run, network):
        abandon(pending_run)
        assert not CheckResult.objects.exists()

        ((run, _),) = recover_stale_runs()
        finished = execute_run(run.id, transport=network.transport())

        assert finished.status == RunStatus.COMPLETED
        assert network.calls == 1
        assert CheckResult.objects.count() == 1

    def test_the_recovered_run_is_the_same_row(self, pending_run, network):
        abandon(pending_run)

        ((run, _),) = recover_stale_runs()
        execute_run(run.id, transport=network.transport())

        assert MonitorRun.objects.count() == 1
        assert MonitorRun.objects.get().id == pending_run.id


class TestResumeWithResult:
    """B. an observation exists, so the network must not run again."""

    @pytest.fixture
    def abandoned_with_result(self, pending_run, network, monkeypatch):
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'process_recorded_result',
            lambda rid: (_ for _ in ()).throw(RuntimeError('died after observing')),
        )
        with pytest.raises(RuntimeError):
            execute_run(pending_run.id, transport=network.transport())
        monkeypatch.undo()

        MonitorRun.objects.filter(pk=pending_run.id).update(
            lease_expires_at=timezone.now() - timedelta(minutes=5)
        )
        return MonitorRun.objects.get(pk=pending_run.id)

    def test_the_network_is_not_contacted_again(self, abandoned_with_result, network):
        assert network.calls == 1

        ((run, _),) = recover_stale_runs()
        execute_run(run.id, transport=network.transport())

        assert network.calls == 1

    def test_the_existing_result_is_incident_processed(self, abandoned_with_result, network):
        result_id = abandoned_with_result.check_result_id

        ((run, _),) = recover_stale_runs()
        execute_run(run.id, transport=network.transport())

        result = CheckResult.objects.get(pk=result_id)
        assert result.incident_processed_at is not None

    def test_the_same_run_completes(self, abandoned_with_result, network):
        ((run, _),) = recover_stale_runs()
        finished = execute_run(run.id, transport=network.transport())

        assert finished.id == abandoned_with_result.id
        assert finished.status == RunStatus.COMPLETED
        assert finished.check_result_id == abandoned_with_result.check_result_id

    def test_only_one_observation_ever_exists(self, abandoned_with_result, network):
        ((run, _),) = recover_stale_runs()
        execute_run(run.id, transport=network.transport())

        assert CheckResult.objects.count() == 1


class TestDuplicateRecovery:
    def test_a_second_scan_is_debounced(self, pending_run):
        abandon(pending_run)

        first = recover_stale_runs()
        second = recover_stale_runs()

        assert len(first) == 1
        assert second == []

    def test_a_still_abandoned_run_is_retried_after_the_debounce(self, pending_run):
        abandon(pending_run)
        recover_stale_runs()

        later = timezone.now() + RECOVERY_DEBOUNCE + timedelta(seconds=1)
        again = recover_stale_runs(now=later)

        assert len(again) == 1

    def test_repeated_scans_create_no_extra_runs(self, pending_run):
        abandon(pending_run)

        for _ in range(5):
            recover_stale_runs()

        assert MonitorRun.objects.count() == 1

    def test_duplicate_recovery_delivery_cannot_duplicate_a_result(
        self, abandoned_with_result_factory, network
    ):
        run = abandoned_with_result_factory()

        for _ in range(4):
            execute_run(run.id, transport=network.transport())

        assert network.calls == 1
        assert CheckResult.objects.count() == 1

    def test_slot_uniqueness_survives_recovery(self, pending_run, network):
        abandon(pending_run)
        recover_stale_runs()
        execute_run(pending_run.id, transport=network.transport())

        assert (
            MonitorRun.objects.filter(
                monitor_id=pending_run.monitor_id, scheduled_for=pending_run.scheduled_for
            ).count()
            == 1
        )


@pytest.fixture
def abandoned_with_result_factory(pending_run, network, monkeypatch):
    def make():
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'process_recorded_result',
            lambda rid: (_ for _ in ()).throw(RuntimeError('died after observing')),
        )
        with pytest.raises(RuntimeError):
            execute_run(pending_run.id, transport=network.transport())
        monkeypatch.undo()
        MonitorRun.objects.filter(pk=pending_run.id).update(
            lease_expires_at=timezone.now() - timedelta(minutes=5)
        )
        return MonitorRun.objects.get(pk=pending_run.id)

    return make


class TestDispatcherIntegration:
    def test_the_dispatcher_recovers_and_dispatches(self, website, sent):
        stale_monitor = due_monitor(website)
        ((stale_run, _),) = claim_due_monitors()
        abandon(stale_run)
        due_monitor(website)

        counts = tasks.dispatch_due_monitors()

        assert counts == {'dispatched': 1, 'recovered': 1}
        assert len(sent) == 2
        assert [str(stale_run.id)] in [message['args'] for message in sent]
        assert stale_monitor.pk

    def test_recovery_routes_by_monitor_type(self, website, sent):
        due_monitor(website, monitor_type=MonitorType.BROWSER)
        ((run, _),) = claim_due_monitors()
        abandon(run)

        recovered = recover_stale_runs()

        assert recovered[0][1] == BROWSER_QUEUE

    def test_an_http_recovery_routes_to_the_http_queue(self, website, sent):
        due_monitor(website, monitor_type=MonitorType.HTTP)
        ((run, _),) = claim_due_monitors()
        abandon(run)

        assert recover_stale_runs()[0][1] == HTTP_QUEUE

    def test_recovery_messages_carry_only_the_run_id(self, website, sent):
        due_monitor(website)
        ((run, _),) = claim_due_monitors()
        abandon(run)

        tasks.dispatch_due_monitors()

        assert sent[0]['args'] == [str(run.id)]


class TestBrokerOutage:
    def test_a_failed_send_leaves_the_run_recoverable(self, website, monkeypatch):
        due_monitor(website)
        ((run, _),) = claim_due_monitors()
        abandon(run)

        def broker_is_down(*args, **kwargs):
            raise ConnectionError('Error 111 connecting to localhost:6379')

        monkeypatch.setattr(tasks.execute_monitor_run, 'apply_async', broker_is_down)

        counts = tasks.dispatch_due_monitors()

        assert counts == {'dispatched': 0, 'recovered': 0}
        stored = MonitorRun.objects.get(pk=run.id)
        assert stored.status == RunStatus.RUNNING
        assert stored.check_result_id is None

    def test_a_failed_send_creates_no_observation_or_incident(self, website, monkeypatch):
        due_monitor(website)
        ((run, _),) = claim_due_monitors()
        abandon(run)
        monkeypatch.setattr(
            tasks.execute_monitor_run,
            'apply_async',
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError('broker down')),
        )

        tasks.dispatch_due_monitors()

        assert not CheckResult.objects.exists()
        assert not Incident.objects.exists()

    def test_the_run_is_picked_up_on_a_later_pass(self, website, monkeypatch, sent):
        due_monitor(website)
        ((run, _),) = claim_due_monitors()
        abandon(run)

        monkeypatch.setattr(
            tasks.execute_monitor_run,
            'apply_async',
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError('broker down')),
        )
        tasks.dispatch_due_monitors()

        # Broker back, and past the debounce window.
        monkeypatch.undo()
        later = timezone.now() + RECOVERY_DEBOUNCE + timedelta(seconds=1)

        assert len(recover_stale_runs(now=later)) == 1

    def test_one_failed_send_does_not_abort_the_rest(self, website, monkeypatch):
        for _ in range(3):
            due_monitor(website)
        attempts = {'n': 0}

        def flaky(args=None, queue=None, **kwargs):
            attempts['n'] += 1
            if attempts['n'] == 1:
                raise ConnectionError('transient')

        monkeypatch.setattr(tasks.execute_monitor_run, 'apply_async', flaky)

        counts = tasks.dispatch_due_monitors()

        assert attempts['n'] == 3
        assert counts['dispatched'] == 2
