"""Incident state machine tests.

Results are fed in one at a time through process_check_result, exactly as the
runner does, so every test exercises the real transition path rather than a
reconstructed one.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from incidents.services import process_check_result
from monitors.models import CheckResult, ErrorType

pytestmark = pytest.mark.django_db


class Sequence:
    """Feeds checks to one monitor, one minute apart, in order."""

    def __init__(self, monitor, start=None):
        self.monitor = monitor
        self.clock = start or timezone.now() - timedelta(hours=2)

    def add(self, is_success, **fields):
        self.clock += timedelta(minutes=1)
        result = CheckResult.objects.create(
            monitor=self.monitor,
            checked_at=self.clock,
            is_success=is_success,
            **fields,
        )
        process_check_result(result)
        return result

    def fail(self, error_type=ErrorType.TIMEOUT, message='timed out', status_code=None):
        return self.add(
            False, error_type=error_type, error_message=message, status_code=status_code
        )

    def succeed(self, status_code=200):
        return self.add(True, status_code=status_code)


@pytest.fixture
def sequence(monitor):
    return Sequence(monitor)


def open_incidents(monitor):
    return Incident.objects.filter(monitor=monitor, status=IncidentStatus.OPEN)


class TestOpening:
    def test_a_single_failure_does_not_open_an_incident(self, sequence, monitor):
        sequence.fail()

        assert not Incident.objects.exists()

    def test_two_consecutive_failures_open_one_incident(self, sequence, monitor):
        sequence.fail()
        sequence.fail()

        assert Incident.objects.count() == 1
        assert open_incidents(monitor).count() == 1

    def test_started_at_is_the_first_failure_not_the_confirming_one(self, sequence):
        first = sequence.fail()
        second = sequence.fail()

        incident = Incident.objects.get()
        assert incident.started_at == first.checked_at
        assert incident.started_at < second.checked_at

    def test_initial_metadata_comes_from_the_first_failure(self, sequence):
        sequence.fail(error_type=ErrorType.DNS_ERROR, message='could not resolve')
        sequence.fail(error_type=ErrorType.TIMEOUT, message='timed out')

        incident = Incident.objects.get()
        assert incident.failure_type == ErrorType.DNS_ERROR
        assert incident.initial_error_message == 'could not resolve'

    def test_latest_metadata_comes_from_the_newest_failure(self, sequence):
        sequence.fail(error_type=ErrorType.DNS_ERROR, message='could not resolve')
        sequence.fail(error_type=ErrorType.HTTP_ERROR, message='HTTP 503', status_code=503)

        incident = Incident.objects.get()
        assert incident.latest_error_message == 'HTTP 503'
        assert incident.latest_status_code == 503

    def test_initial_status_code_is_preserved_separately(self, sequence):
        sequence.fail(error_type=ErrorType.HTTP_ERROR, message='HTTP 500', status_code=500)
        sequence.fail(error_type=ErrorType.HTTP_ERROR, message='HTTP 503', status_code=503)

        incident = Incident.objects.get()
        assert incident.initial_status_code == 500
        assert incident.latest_status_code == 503

    def test_failure_count_starts_at_the_confirmed_run_length(self, sequence):
        sequence.fail()
        sequence.fail()

        assert Incident.objects.get().failure_count == 2

    def test_a_missing_error_type_falls_back_to_unknown(self, sequence):
        sequence.add(False, error_message='no type recorded')
        sequence.add(False, error_message='no type recorded')

        assert Incident.objects.get().failure_type == ErrorType.UNKNOWN_ERROR


class TestOngoingFailures:
    def test_further_failures_do_not_create_more_incidents(self, sequence, monitor):
        for _ in range(5):
            sequence.fail()

        assert Incident.objects.count() == 1
        assert open_incidents(monitor).count() == 1

    def test_failure_count_tracks_every_failure(self, sequence):
        for _ in range(5):
            sequence.fail()

        assert Incident.objects.get().failure_count == 5

    def test_latest_details_follow_the_newest_failure(self, sequence):
        sequence.fail(error_type=ErrorType.TIMEOUT, message='timed out')
        sequence.fail(error_type=ErrorType.TIMEOUT, message='timed out')
        sequence.fail(error_type=ErrorType.CONNECTION_ERROR, message='connection refused')

        incident = Incident.objects.get()
        assert incident.latest_error_message == 'connection refused'
        # The originating cause is what the incident is; it does not drift.
        assert incident.failure_type == ErrorType.TIMEOUT
        assert incident.initial_error_message == 'timed out'


class TestRecovery:
    def test_one_success_leaves_the_incident_open(self, sequence, monitor):
        sequence.fail()
        sequence.fail()
        sequence.succeed()

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.resolved_at is None
        assert incident.recovery_count == 1

    def test_two_consecutive_successes_resolve_it(self, sequence, monitor):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.RESOLVED
        assert not open_incidents(monitor).exists()

    def test_resolved_at_is_the_second_success(self, sequence):
        sequence.fail()
        sequence.fail()
        first_success = sequence.succeed()
        second_success = sequence.succeed()

        incident = Incident.objects.get()
        assert incident.resolved_at == second_success.checked_at
        assert incident.resolved_at > first_success.checked_at

    def test_resolution_preserves_the_failure_history(self, sequence):
        sequence.fail(error_type=ErrorType.DNS_ERROR, message='could not resolve')
        sequence.fail(error_type=ErrorType.HTTP_ERROR, message='HTTP 500', status_code=500)
        sequence.succeed()
        sequence.succeed()

        incident = Incident.objects.get()
        assert incident.failure_type == ErrorType.DNS_ERROR
        assert incident.initial_error_message == 'could not resolve'
        assert incident.latest_error_message == 'HTTP 500'
        assert incident.failure_count == 2

    def test_a_failure_between_successes_resets_recovery_progress(self, sequence, monitor):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.fail()

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.recovery_count == 0
        assert incident.failure_count == 3

    def test_recovery_must_start_over_after_a_reset(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.fail()
        sequence.succeed()

        assert Incident.objects.get().status == IncidentStatus.OPEN

        sequence.succeed()

        assert Incident.objects.get().status == IncidentStatus.RESOLVED

    def test_successes_with_no_open_incident_do_nothing(self, sequence):
        sequence.succeed()
        sequence.succeed()
        sequence.succeed()

        assert not Incident.objects.exists()


class TestSequences:
    """The exact sequences that define correct behaviour."""

    def test_fail_success_fail_opens_nothing(self, sequence):
        sequence.fail()
        sequence.succeed()
        sequence.fail()

        assert not Incident.objects.exists()

    def test_fail_fail_opens(self, sequence):
        sequence.fail()
        sequence.fail()

        assert Incident.objects.get().status == IncidentStatus.OPEN

    def test_fail_fail_success_stays_open(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()

        assert Incident.objects.get().status == IncidentStatus.OPEN

    def test_fail_fail_success_fail_stays_open(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.fail()

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.recovery_count == 0

    def test_fail_fail_success_success_resolves(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()

        assert Incident.objects.get().status == IncidentStatus.RESOLVED

    def test_a_later_outage_creates_a_second_incident(self, sequence, monitor):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()
        sequence.fail()
        sequence.fail()

        assert Incident.objects.count() == 2
        assert open_incidents(monitor).count() == 1
        first, second = Incident.objects.order_by('started_at')
        assert first.status == IncidentStatus.RESOLVED
        assert second.status == IncidentStatus.OPEN

    def test_a_single_failure_after_resolution_does_not_reopen(self, sequence):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()
        sequence.fail()

        assert Incident.objects.count() == 1
        assert Incident.objects.get().status == IncidentStatus.RESOLVED

    def test_alternating_results_never_open_an_incident(self, sequence):
        for _ in range(6):
            sequence.fail()
            sequence.succeed()

        assert not Incident.objects.exists()

    def test_monitors_do_not_interfere_with_each_other(self, monitor, other_monitor):
        one = Sequence(monitor)
        two = Sequence(other_monitor)

        one.fail()
        two.fail()
        two.succeed()
        one.fail()

        assert Incident.objects.count() == 1
        assert Incident.objects.get().monitor == monitor


class TestIdempotency:
    def test_reprocessing_the_opening_result_does_not_duplicate_the_incident(self, sequence):
        sequence.fail()
        second = sequence.fail()

        process_check_result(second)
        process_check_result(second)

        assert Incident.objects.count() == 1
        assert Incident.objects.get().failure_count == 2

    def test_reprocessing_a_failure_does_not_double_increment(self, sequence):
        sequence.fail()
        sequence.fail()
        third = sequence.fail()

        assert Incident.objects.get().failure_count == 3

        process_check_result(third)

        assert Incident.objects.get().failure_count == 3

    def test_reprocessing_a_success_does_not_advance_recovery(self, sequence):
        sequence.fail()
        sequence.fail()
        success = sequence.succeed()

        assert Incident.objects.get().recovery_count == 1

        process_check_result(success)
        process_check_result(success)

        incident = Incident.objects.get()
        assert incident.recovery_count == 1
        assert incident.status == IncidentStatus.OPEN

    def test_processing_stamps_the_result(self, sequence):
        result = sequence.fail()

        result.refresh_from_db()
        assert result.incident_processed_at is not None

    def test_reprocessing_returns_the_current_open_incident(self, sequence):
        sequence.fail()
        second = sequence.fail()

        assert process_check_result(second) == Incident.objects.get()

    def test_the_database_rejects_a_second_open_incident(self, sequence, monitor):
        from django.db import IntegrityError

        sequence.fail()
        sequence.fail()
        opened = Incident.objects.get()

        with pytest.raises(IntegrityError):
            Incident.objects.create(
                monitor=monitor,
                status=IncidentStatus.OPEN,
                started_at=opened.started_at,
                failure_type=ErrorType.TIMEOUT,
            )

    def test_a_resolved_incident_does_not_block_a_new_open_one(self, sequence, monitor):
        sequence.fail()
        sequence.fail()
        sequence.succeed()
        sequence.succeed()
        sequence.fail()
        sequence.fail()

        assert Incident.objects.filter(status=IncidentStatus.RESOLVED).count() == 1
        assert Incident.objects.filter(status=IncidentStatus.OPEN).count() == 1
