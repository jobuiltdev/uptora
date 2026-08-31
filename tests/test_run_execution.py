"""Worker execution: leases, duplicate delivery, retries and crash boundaries.

Celery delivers at least once, so every test here asks the same question in a
different way: what happens when this run is executed more than once?

The network is a counting MockTransport, so "was the target contacted again" is
a number rather than an inference.
"""

import socket
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from monitors import ssrf
from monitors.checks import tls
from monitors.models import CheckResult, ErrorType, Monitor, MonitorRun, RunStatus
from monitors.scheduling import (
    claim_due_monitors,
    claim_run,
    execute_run,
    lease_duration,
    mark_failed_internal,
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
    """A transport that counts every request that reaches the wire."""

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


def run_it(run, network, **kwargs):
    return execute_run(run.id, transport=network.transport(), **kwargs)


class TestHappyPath:
    def test_a_run_produces_exactly_one_result(self, pending_run, network):
        run = run_it(pending_run, network)

        assert run.status == RunStatus.COMPLETED
        assert network.calls == 1
        assert CheckResult.objects.count() == 1
        assert run.check_result_id == CheckResult.objects.get().pk

    def test_the_run_records_its_timings(self, pending_run, network):
        run = run_it(pending_run, network)

        assert run.claimed_at is not None
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.attempt_count == 1

    def test_the_lease_is_released_on_completion(self, pending_run, network):
        run = run_it(pending_run, network)

        assert run.lease_expires_at is None

    def test_incident_state_is_updated(self, pending_run, network):
        run = run_it(pending_run, network)

        run.check_result.refresh_from_db()
        assert run.check_result.incident_processed_at is not None

    def test_a_target_failure_still_records_an_observation(self, pending_run):
        failing = Network(status_code=503)

        run = run_it(pending_run, failing)

        assert run.status == RunStatus.COMPLETED
        assert run.check_result.is_success is False
        assert run.check_result.error_type == ErrorType.HTTP_ERROR


class TestDuplicateDelivery:
    def test_a_second_delivery_does_not_contact_the_target_again(self, pending_run, network):
        run_it(pending_run, network)

        run_it(pending_run, network)

        assert network.calls == 1
        assert CheckResult.objects.count() == 1

    def test_a_completed_run_is_a_no_op(self, pending_run, network):
        run_it(pending_run, network)
        before = MonitorRun.objects.get(pk=pending_run.id).finished_at

        run = run_it(pending_run, network)

        assert run.status == RunStatus.COMPLETED
        assert run.finished_at == before
        assert run.attempt_count == 1

    def test_many_deliveries_still_produce_one_observation(self, pending_run, network):
        for _ in range(5):
            run_it(pending_run, network)

        assert network.calls == 1
        assert CheckResult.objects.count() == 1


class TestLeases:
    def test_a_live_lease_cannot_be_stolen(self, pending_run):
        token, _ = claim_run(pending_run.id)
        assert token is not None

        second_token, run = claim_run(pending_run.id)

        assert second_token is None
        assert run.status == RunStatus.RUNNING

    def test_a_leased_run_is_not_executed_by_a_second_delivery(self, pending_run, network):
        claim_run(pending_run.id)

        run_it(pending_run, network)

        assert network.calls == 0

    def test_an_expired_lease_may_be_reclaimed(self, pending_run):
        token, _ = claim_run(pending_run.id)
        MonitorRun.objects.filter(pk=pending_run.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        second_token, run = claim_run(pending_run.id)

        assert second_token is not None
        assert second_token != token
        assert run.attempt_count == 2

    def test_the_lease_outlasts_the_monitor_timeout(self, website):
        monitor = due_monitor(website, timeout_seconds=30)

        assert lease_duration(monitor) > timedelta(seconds=30)

    def test_reclaiming_after_a_result_exists_does_not_recontact(self, pending_run, network):
        """The at-least-once edge, bounded: once the observation is stored, a
        reclaim resumes rather than re-probes."""
        run_it(pending_run, network)
        MonitorRun.objects.filter(pk=pending_run.id).update(
            status=RunStatus.RUNNING, lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        run = run_it(pending_run, network)

        assert network.calls == 1
        assert run.status == RunStatus.COMPLETED


class TestRetryBoundary:
    """Incident processing must never be retried by re-running the network."""

    def test_a_failed_incident_stage_is_retried_against_the_same_result(
        self, pending_run, network, monkeypatch
    ):
        import monitors.scheduling as scheduling

        def explode(check_result_id):
            raise RuntimeError('incident engine blew up')

        monkeypatch.setattr(scheduling, 'process_recorded_result', explode)

        with pytest.raises(RuntimeError):
            run_it(pending_run, network)

        # The observation exists and belongs to the run, unprocessed.
        assert network.calls == 1
        assert CheckResult.objects.count() == 1
        stored = MonitorRun.objects.get(pk=pending_run.id)
        assert stored.check_result_id is not None
        assert stored.status == RunStatus.RUNNING
        assert stored.internal_error

        # The retry resumes at the incident stage.
        monkeypatch.undo()
        run = run_it(pending_run, network)

        assert network.calls == 1
        assert CheckResult.objects.count() == 1
        assert run.status == RunStatus.COMPLETED
        assert run.check_result_id == stored.check_result_id
        run.check_result.refresh_from_db()
        assert run.check_result.incident_processed_at is not None

    def test_the_lease_is_released_so_a_retry_can_resume(self, pending_run, network, monkeypatch):
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'process_recorded_result',
            lambda check_result_id: (_ for _ in ()).throw(RuntimeError('boom')),
        )

        with pytest.raises(RuntimeError):
            run_it(pending_run, network)

        stored = MonitorRun.objects.get(pk=pending_run.id)
        assert stored.lease_expires_at <= timezone.now()

    def test_a_failing_incident_stage_never_writes_a_second_observation(
        self, pending_run, network, monkeypatch
    ):
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'process_recorded_result',
            lambda check_result_id: (_ for _ in ()).throw(RuntimeError('boom')),
        )

        for _ in range(3):
            with pytest.raises(RuntimeError):
                run_it(pending_run, network)

        assert network.calls == 1
        assert CheckResult.objects.count() == 1


class TestCrashBoundaries:
    """Where a process dies changes what recovery costs."""

    def test_a_dying_before_the_network_costs_nothing(self, pending_run, network, monkeypatch):
        """A. claimed, then died. The lease expires and the run is reclaimed."""
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'observe',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('worker died')),
        )

        with pytest.raises(RuntimeError):
            run_it(pending_run, network)

        assert network.calls == 0
        assert not CheckResult.objects.exists()

        monkeypatch.undo()
        MonitorRun.objects.filter(pk=pending_run.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        run = run_it(pending_run, network)

        assert run.status == RunStatus.COMPLETED
        assert network.calls == 1

    def test_b_dying_after_the_request_loses_the_observation(
        self, pending_run, network, monkeypatch
    ):
        """B. the honest edge: the request went out, nothing recorded it.

        A reclaim makes the request a second time. This is the case that cannot
        be made exactly-once, and the test exists to state that plainly rather
        than to pretend otherwise.
        """
        import monitors.scheduling as scheduling

        real_observe = scheduling.observe

        def observe_then_die(*args, **kwargs):
            real_observe(*args, **kwargs)
            raise RuntimeError('worker died holding the observation')

        monkeypatch.setattr(scheduling, 'observe', observe_then_die)

        with pytest.raises(RuntimeError):
            run_it(pending_run, network)

        assert network.calls == 1
        assert not CheckResult.objects.exists()

        monkeypatch.undo()
        MonitorRun.objects.filter(pk=pending_run.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        run_it(pending_run, network)

        # Contacted twice. Documented and unavoidable.
        assert network.calls == 2
        assert CheckResult.objects.count() == 1

    def test_c_the_result_and_its_link_commit_together(self, pending_run, network):
        """C. cannot happen: one transaction covers both writes."""
        run_it(pending_run, network)

        result = CheckResult.objects.get()
        assert MonitorRun.objects.get(pk=pending_run.id).check_result_id == result.pk

    def test_e_dying_before_completion_reprocesses_harmlessly(
        self, pending_run, network, monkeypatch
    ):
        """E. incident processing done, crash before COMPLETED."""
        import monitors.scheduling as scheduling

        real = scheduling.process_recorded_result
        calls = {'n': 0}

        def process_then_die(check_result_id):
            calls['n'] += 1
            real(check_result_id)
            if calls['n'] == 1:
                raise RuntimeError('died before completing')

        monkeypatch.setattr(scheduling, 'process_recorded_result', process_then_die)

        with pytest.raises(RuntimeError):
            run_it(pending_run, network)

        run = run_it(pending_run, network)

        assert run.status == RunStatus.COMPLETED
        assert network.calls == 1
        assert CheckResult.objects.count() == 1


class TestInternalFailures:
    """Uptora breaking must never look like the customer's site breaking."""

    def test_an_internal_failure_writes_no_observation(self, pending_run, network, monkeypatch):
        import monitors.scheduling as scheduling

        monkeypatch.setattr(
            scheduling,
            'observe',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('database is on fire')),
        )

        with pytest.raises(RuntimeError):
            run_it(pending_run, network)

        assert not CheckResult.objects.exists()
        assert not Incident.objects.exists()

    def test_an_internal_failure_is_recorded_operationally(self, pending_run):
        mark_failed_internal(pending_run.id, 'RuntimeError: broker unreachable')

        run = MonitorRun.objects.get(pk=pending_run.id)
        assert run.status == RunStatus.FAILED_INTERNAL
        assert 'broker unreachable' in run.internal_error
        assert run.finished_at is not None

    def test_marking_internal_failure_creates_no_incident(self, pending_run):
        mark_failed_internal(pending_run.id, 'boom')

        assert not Incident.objects.exists()
        assert not CheckResult.objects.exists()

    def test_a_completed_run_is_not_overwritten(self, pending_run, network):
        run_it(pending_run, network)

        mark_failed_internal(pending_run.id, 'late arrival')

        assert MonitorRun.objects.get(pk=pending_run.id).status == RunStatus.COMPLETED

    def test_the_internal_error_is_capped(self, pending_run):
        mark_failed_internal(pending_run.id, 'x' * 5000)

        assert len(MonitorRun.objects.get(pk=pending_run.id).internal_error) <= 500


class TestDisableDuringScheduling:
    def test_a_pending_run_is_cancelled_when_the_monitor_is_disabled(self, pending_run, network):
        Monitor.objects.filter(pk=pending_run.monitor_id).update(is_enabled=False)

        run = run_it(pending_run, network)

        assert run.status == RunStatus.CANCELLED
        assert network.calls == 0
        assert not CheckResult.objects.exists()

    def test_a_running_run_is_allowed_to_finish(self, pending_run, network, monkeypatch):
        """Disabling mid-check does not abandon work already under way."""
        import monitors.scheduling as scheduling

        real_observe = scheduling.observe

        def disable_then_observe(monitor, **kwargs):
            Monitor.objects.filter(pk=monitor.pk).update(is_enabled=False)
            return real_observe(monitor, **kwargs)

        monkeypatch.setattr(scheduling, 'observe', disable_then_observe)

        run = run_it(pending_run, network)

        assert run.status == RunStatus.COMPLETED
        assert CheckResult.objects.count() == 1

    def test_a_cancelled_run_stays_cancelled_on_redelivery(self, pending_run, network):
        Monitor.objects.filter(pk=pending_run.monitor_id).update(is_enabled=False)
        run_it(pending_run, network)

        run = run_it(pending_run, network)

        assert run.status == RunStatus.CANCELLED
        assert network.calls == 0


class TestIncidentIntegration:
    def test_two_scheduled_failures_open_an_incident(self, website):
        monitor = due_monitor(website)
        failing = Network(status_code=500)

        for _ in range(2):
            Monitor.objects.filter(pk=monitor.pk).update(
                next_check_at=timezone.now() - timedelta(minutes=1)
            )
            ((run, _),) = claim_due_monitors()
            run_it(run, failing)

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.failure_count == 2

    def test_scheduled_recoveries_resolve_it(self, website):
        monitor = due_monitor(website)

        def cycle(net):
            Monitor.objects.filter(pk=monitor.pk).update(
                next_check_at=timezone.now() - timedelta(minutes=1)
            )
            ((run, _),) = claim_due_monitors()
            run_it(run, net)

        failing, healthy = Network(status_code=500), Network()
        cycle(failing)
        cycle(failing)
        cycle(healthy)
        assert Incident.objects.get().status == IncidentStatus.OPEN

        cycle(healthy)
        assert Incident.objects.get().status == IncidentStatus.RESOLVED
