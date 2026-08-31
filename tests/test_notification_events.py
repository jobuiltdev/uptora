"""Notification events: exactly one per confirmed transition, under replay.

Incident state is recomputed on every check, so the question these tests keep
asking is: does replaying the same history emit a second alert?
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from incidents.services import process_check_result
from monitors.models import CheckResult, ErrorType
from notifications.models import EventType, NotificationDelivery, NotificationEvent

pytestmark = pytest.mark.django_db


class Sequence:
    """Feeds checks to one monitor, a minute apart, in order."""

    def __init__(self, monitor, start=None):
        self.monitor = monitor
        self.clock = start or timezone.now() - timedelta(hours=2)
        self.results = []

    def add(self, is_success, process=True, **fields):
        self.clock += timedelta(minutes=1)
        result = CheckResult.objects.create(
            monitor=self.monitor, checked_at=self.clock, is_success=is_success, **fields
        )
        self.results.append(result)
        if process:
            process_check_result(result)
        return result

    def fail(self, **kwargs):
        return self.add(False, error_type=ErrorType.TIMEOUT, error_message='timed out', **kwargs)

    def succeed(self, **kwargs):
        return self.add(True, status_code=200, **kwargs)


@pytest.fixture
def sequence(monitor):
    return Sequence(monitor)


def events(event_type=None):
    queryset = NotificationEvent.objects.all()
    if event_type:
        queryset = queryset.filter(event_type=event_type)
    return queryset


class TestOpening:
    def test_one_failure_announces_nothing(self, sequence):
        sequence.fail()

        assert not events().exists()

    def test_the_confirming_failure_opens_exactly_one_event(self, sequence):
        sequence.fail()
        sequence.fail()

        assert events().count() == 1
        assert events().get().event_type == EventType.INCIDENT_OPENED

    def test_further_failures_announce_nothing_more(self, sequence):
        for _ in range(6):
            sequence.fail()

        assert events(EventType.INCIDENT_OPENED).count() == 1

    def test_the_event_points_at_the_incident(self, sequence):
        sequence.fail()
        sequence.fail()

        incident = Incident.objects.get()
        assert events().get().incident_id == incident.pk

    def test_the_event_records_when_the_outage_began(self, sequence):
        first = sequence.fail()
        sequence.fail()

        assert events().get().occurred_at == first.checked_at


class TestResolving:
    def test_one_success_announces_nothing(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()

        assert events(EventType.INCIDENT_RESOLVED).count() == 0

    def test_the_confirming_success_resolves_exactly_once(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()

        assert events(EventType.INCIDENT_RESOLVED).count() == 1

    def test_further_successes_announce_nothing_more(self, sequence):
        sequence.fail()
        sequence.fail()
        for _ in range(5):
            sequence.succeed()

        assert events(EventType.INCIDENT_RESOLVED).count() == 1

    def test_a_full_cycle_produces_exactly_two_events(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()

        assert events().count() == 2

    def test_the_resolved_event_records_the_recovery_time(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        second = sequence.succeed()

        resolved = events(EventType.INCIDENT_RESOLVED).get()
        assert resolved.occurred_at == second.checked_at

    def test_a_second_outage_announces_again(self, sequence):
        for _ in (1, 2):
            sequence.fail()
        sequence.succeed()
        sequence.succeed()
        sequence.fail()
        sequence.fail()

        assert events(EventType.INCIDENT_OPENED).count() == 2
        assert events(EventType.INCIDENT_RESOLVED).count() == 1


class TestReplay:
    """Incident state is derived, so the same stream may be folded repeatedly."""

    def test_reprocessing_every_result_adds_nothing(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()
        before = events().count()

        for result in sequence.results:
            process_check_result(result)
            process_check_result(result)

        assert events().count() == before == 2

    def test_reprocessing_the_opening_result_does_not_realert(self, sequence):
        sequence.fail()
        second = sequence.fail()

        for _ in range(5):
            process_check_result(second)

        assert events(EventType.INCIDENT_OPENED).count() == 1

    def test_out_of_order_processing_converges_on_two_events(self, monitor):
        """The whole history persisted first, then processed backwards."""
        sequence = Sequence(monitor)
        for is_success in (False, False, True, True):
            sequence.add(is_success, process=False, status_code=200 if is_success else 500)

        for result in reversed(sequence.results):
            process_check_result(result)

        assert events(EventType.INCIDENT_RESOLVED).count() == 1
        assert Incident.objects.count() == 1

    def test_a_history_discovered_after_the_fact_announces_only_recovery(self, monitor):
        """An outage that had already recovered before Uptora folded it.

        Reachable only by replaying a complete history out of order. Announcing
        "problem detected" and "recovered" in the same instant would be noise,
        so only the resolution is reported.
        """
        sequence = Sequence(monitor)
        for is_success in (False, False, True, True):
            sequence.add(is_success, process=False, status_code=200 if is_success else 500)

        process_check_result(sequence.results[-1])

        assert Incident.objects.get().status == IncidentStatus.RESOLVED
        assert events(EventType.INCIDENT_RESOLVED).count() == 1
        assert events(EventType.INCIDENT_OPENED).count() == 0

    def test_one_delivery_per_event_under_replay(self, sequence):
        sequence.fail()
        sequence.fail()

        for _ in range(4):
            process_check_result(sequence.results[-1])

        assert NotificationDelivery.objects.count() == 1


class TestWithdrawal:
    """A late result can say an outage never happened."""

    def test_a_withdrawn_incident_keeps_its_announcement_on_record(self, monitor):
        base = timezone.now() - timedelta(hours=1)
        first = CheckResult.objects.create(
            monitor=monitor, checked_at=base, is_success=False, error_message='down'
        )
        third = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=2),
            is_success=False,
            error_message='down',
        )
        process_check_result(first)
        process_check_result(third)
        assert events(EventType.INCIDENT_OPENED).count() == 1

        # The success that was always between them arrives late.
        between = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=1),
            is_success=True,
            status_code=200,
        )
        process_check_result(between)

        # The incident is gone, but the alert already went out, so its record
        # survives with the link cleared rather than being erased.
        assert not Incident.objects.exists()
        assert events().count() == 1
        assert events().get().incident_id is None

    def test_a_never_confirmed_outage_announces_nothing(self, sequence):
        sequence.fail()
        sequence.succeed()
        sequence.fail()

        assert not events().exists()


class TestTransactionalConsistency:
    def test_a_rolled_back_transition_leaves_no_event(self, sequence, monkeypatch):
        """If the incident write is undone, the alert must be undone with it."""
        import incidents.services as services

        real_stamp = services.stamp_processed

        def stamp_then_fail(check_result):
            real_stamp(check_result)
            raise RuntimeError('database went away mid-transaction')

        # The first failure confirms nothing, so let it through untouched; the
        # second is the one that would open an incident.
        sequence.fail()
        monkeypatch.setattr(services, 'stamp_processed', stamp_then_fail)

        with pytest.raises(RuntimeError):
            sequence.fail()

        # Neither the incident nor its announcement survived.
        assert not Incident.objects.exists()
        assert not events().exists()
        assert not NotificationDelivery.objects.exists()

    def test_the_transition_and_event_commit_together(self, sequence, monkeypatch):
        import incidents.services as services

        real_stamp = services.stamp_processed

        sequence.fail()
        monkeypatch.setattr(
            services,
            'stamp_processed',
            lambda cr: (_ for _ in ()).throw(RuntimeError('boom')),
        )
        with pytest.raises(RuntimeError):
            sequence.fail()
        monkeypatch.setattr(services, 'stamp_processed', real_stamp)

        # Retrying the same result now succeeds, and announces once.
        process_check_result(sequence.results[-1])

        assert Incident.objects.count() == 1
        assert events(EventType.INCIDENT_OPENED).count() == 1


class TestExecutionPathParity:
    """Where a CheckResult came from must not change what gets announced."""

    def test_a_manual_run_announces_the_same_way(self, monitor, monkeypatch):
        import socket

        import httpx

        from monitors import ssrf
        from monitors.checks import tls
        from monitors.execution import execute_monitor

        monkeypatch.setattr(
            ssrf.socket,
            'getaddrinfo',
            lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))
            ],
        )
        monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)
        transport = httpx.MockTransport(lambda request: httpx.Response(500))

        execute_monitor(monitor, transport=transport)
        assert not events().exists()

        execute_monitor(monitor, transport=transport)

        assert events(EventType.INCIDENT_OPENED).count() == 1

    def test_a_scheduled_run_announces_the_same_way(self, website, monkeypatch):
        import socket

        import httpx

        from monitors import ssrf
        from monitors.checks import tls
        from monitors.models import Monitor
        from monitors.scheduling import claim_due_monitors, execute_run

        monkeypatch.setattr(
            ssrf.socket,
            'getaddrinfo',
            lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))
            ],
        )
        monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)

        monitor = Monitor.objects.create(website=website)
        for _ in range(2):
            Monitor.objects.filter(pk=monitor.pk).update(
                next_check_at=timezone.now() - timedelta(minutes=1)
            )
            ((run, _),) = claim_due_monitors()
            execute_run(run.id, transport=httpx.MockTransport(lambda request: httpx.Response(500)))

        assert events(EventType.INCIDENT_OPENED).count() == 1
