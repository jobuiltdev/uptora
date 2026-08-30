"""Out-of-order processing.

Incident state is derived from the ordered result stream rather than incremented
per arrival, so the order in which results are handed to process_check_result
must not matter. These tests persist a whole sequence first, then process it in
deliberately wrong orders and require the same answer every time.
"""

from datetime import timedelta
from itertools import permutations

import pytest
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from incidents.services import process_check_result
from monitors.models import CheckResult, ErrorType

pytestmark = pytest.mark.django_db

BASE_TIME = timezone.now() - timedelta(days=1)

DERIVED = (
    'status',
    'started_at',
    'resolved_at',
    'failure_type',
    'initial_error_message',
    'initial_status_code',
    'latest_error_message',
    'latest_status_code',
    'failure_count',
    'recovery_count',
)


def build(monitor, outcomes):
    """Persist a sequence of results one minute apart, without processing any.

    Mirrors what record_check leaves behind: the rows are committed and visible,
    and the incident stage has not run for any of them yet.
    """
    results = []
    for index, is_success in enumerate(outcomes):
        results.append(
            CheckResult.objects.create(
                monitor=monitor,
                checked_at=BASE_TIME + timedelta(minutes=index),
                is_success=is_success,
                status_code=200 if is_success else 500 + index,
                error_type=None if is_success else ErrorType.HTTP_ERROR,
                error_message=None if is_success else f'failure {index}',
            )
        )
    return results


def process_in(results, order):
    for index in order:
        process_check_result(results[index])


def snapshot(monitor):
    """Everything the engine derives, for comparing two runs field by field."""
    return [
        {name: getattr(incident, name) for name in DERIVED}
        for incident in Incident.objects.filter(monitor=monitor).order_by('started_at', 'id')
    ]


SEQUENCES = {
    'fail-fail': (False, False),
    'fail-fail-success-success': (False, False, True, True),
    'fail-fail-success-fail': (False, False, True, False),
    'fail-success-fail': (False, True, False),
}


class TestScenarioA:
    """FAIL #1, FAIL #2 processed newest-first."""

    def test_reversed_order_opens_one_correct_incident(self, monitor):
        results = build(monitor, SEQUENCES['fail-fail'])

        process_in(results, [1, 0])

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.started_at == results[0].checked_at
        assert incident.initial_error_message == 'failure 0'
        assert incident.initial_status_code == 500
        assert incident.latest_error_message == 'failure 1'
        assert incident.latest_status_code == 501
        assert incident.failure_count == 2
        assert incident.recovery_count == 0

    def test_reversed_order_matches_forward_order(self, monitor, other_monitor):
        forward = build(monitor, SEQUENCES['fail-fail'])
        backward = build(other_monitor, SEQUENCES['fail-fail'])

        process_in(forward, [0, 1])
        process_in(backward, [1, 0])

        assert snapshot(monitor) == snapshot(other_monitor)


class TestScenarioB:
    """FAIL, FAIL, SUCCESS, SUCCESS resolves exactly once, however it arrives."""

    @pytest.mark.parametrize('order', [[3, 2, 1, 0], [2, 0, 3, 1], [3, 1, 0, 2], [1, 3, 0, 2]])
    def test_resolves_exactly_once(self, monitor, order):
        results = build(monitor, SEQUENCES['fail-fail-success-success'])

        process_in(results, order)

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.RESOLVED
        assert incident.started_at == results[0].checked_at
        assert incident.resolved_at == results[3].checked_at
        assert incident.failure_count == 2
        assert incident.recovery_count == 2
        assert not Incident.objects.filter(status=IncidentStatus.OPEN).exists()

    def test_no_duplicate_incident_is_left_behind(self, monitor):
        results = build(monitor, SEQUENCES['fail-fail-success-success'])

        process_in(results, [3, 2, 1, 0])

        assert Incident.objects.count() == 1


class TestScenarioC:
    """FAIL, FAIL, SUCCESS, FAIL stays open with recovery progress cleared."""

    @pytest.mark.parametrize('order', [[3, 0, 2, 1], [2, 3, 1, 0], [1, 2, 3, 0], [3, 2, 1, 0]])
    def test_stays_open_with_recovery_reset(self, monitor, order):
        results = build(monitor, SEQUENCES['fail-fail-success-fail'])

        process_in(results, order)

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.resolved_at is None
        assert incident.recovery_count == 0
        assert incident.failure_count == 3
        assert incident.started_at == results[0].checked_at
        assert incident.latest_error_message == 'failure 3'
        assert incident.latest_status_code == 503


class TestScenarioD:
    """Reprocessing already-incorporated results changes nothing."""

    def test_repeating_the_whole_sequence_is_inert(self, monitor):
        results = build(monitor, SEQUENCES['fail-fail-success-fail'])
        process_in(results, range(len(results)))
        before = snapshot(monitor)

        for order in ([0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]):
            process_in(results, order)

        assert snapshot(monitor) == before
        assert Incident.objects.count() == 1

    def test_repeating_the_oldest_result_does_not_reopen_history(self, monitor):
        results = build(monitor, SEQUENCES['fail-fail-success-success'])
        process_in(results, range(len(results)))
        before = snapshot(monitor)

        for _ in range(3):
            process_check_result(results[0])

        assert snapshot(monitor) == before
        assert Incident.objects.get().status == IncidentStatus.RESOLVED

    def test_repeating_the_newest_result_does_not_double_count(self, monitor):
        results = build(monitor, SEQUENCES['fail-fail-success-fail'])
        process_in(results, range(len(results)))

        for _ in range(5):
            process_check_result(results[-1])

        incident = Incident.objects.get()
        assert incident.failure_count == 3
        assert incident.recovery_count == 0

    def test_processing_stamps_every_result_exactly_once(self, monitor):
        results = build(monitor, SEQUENCES['fail-fail-success-fail'])
        process_in(results, range(len(results)))

        stamps = {r.pk: r.incident_processed_at for r in CheckResult.objects.all()}
        assert all(value is not None for value in stamps.values())

        process_in(results, [3, 1, 0, 2])

        assert {r.pk: r.incident_processed_at for r in CheckResult.objects.all()} == stamps


class TestEveryOrdering:
    """The general claim: any permutation lands where in-order processing does."""

    @pytest.mark.parametrize('name', list(SEQUENCES))
    def test_all_permutations_agree_with_in_order(self, monitor, other_monitor, name):
        outcomes = SEQUENCES[name]
        reference = build(monitor, outcomes)
        process_in(reference, range(len(outcomes)))
        expected = snapshot(monitor)

        for order in permutations(range(len(outcomes))):
            Incident.objects.filter(monitor=other_monitor).delete()
            CheckResult.objects.filter(monitor=other_monitor).delete()
            scrambled = build(other_monitor, outcomes)

            process_in(scrambled, order)

            assert snapshot(other_monitor) == expected, f'{name} processed as {order}'


class TestPartialArrival:
    """A result that has not been persisted yet cannot influence the fold.

    This is the honest limit of the model: processing sees the table, so a
    result still in flight is simply not part of the stream yet. Once it lands
    and anything is processed, the state corrects itself.
    """

    def test_state_corrects_itself_once_a_late_result_lands(self, monitor):
        early = CheckResult.objects.create(
            monitor=monitor,
            checked_at=BASE_TIME + timedelta(minutes=1),
            is_success=False,
            error_type=ErrorType.TIMEOUT,
            error_message='failure 1',
        )
        process_check_result(early)
        assert not Incident.objects.exists()

        late = CheckResult.objects.create(
            monitor=monitor,
            checked_at=BASE_TIME,
            is_success=False,
            error_type=ErrorType.DNS_ERROR,
            error_message='failure 0',
        )
        process_check_result(late)

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        # The fold re-read the stream, so the earlier arrival is now the start.
        assert incident.started_at == late.checked_at
        assert incident.failure_type == ErrorType.DNS_ERROR
        assert incident.failure_count == 2

    def test_a_late_success_between_two_failures_withdraws_the_incident(self, monitor):
        """The ordered stream says FAIL, SUCCESS, FAIL, which is not an outage."""
        first = CheckResult.objects.create(
            monitor=monitor, checked_at=BASE_TIME, is_success=False, error_message='failure 0'
        )
        second = CheckResult.objects.create(
            monitor=monitor,
            checked_at=BASE_TIME + timedelta(minutes=2),
            is_success=False,
            error_message='failure 2',
        )
        process_check_result(first)
        process_check_result(second)
        assert Incident.objects.count() == 1

        between = CheckResult.objects.create(
            monitor=monitor,
            checked_at=BASE_TIME + timedelta(minutes=1),
            is_success=True,
            status_code=200,
        )
        process_check_result(between)

        assert not Incident.objects.exists()


class TestOrderingKey:
    """(checked_at, id) is the canonical order, so identical timestamps still sort."""

    def test_results_sharing_a_timestamp_are_ordered_by_id(self, monitor):
        moment = BASE_TIME
        first = CheckResult.objects.create(
            monitor=monitor,
            checked_at=moment,
            is_success=False,
            error_type=ErrorType.DNS_ERROR,
            error_message='failure 0',
        )
        second = CheckResult.objects.create(
            monitor=monitor,
            checked_at=moment,
            is_success=False,
            error_type=ErrorType.TIMEOUT,
            error_message='failure 1',
        )

        process_check_result(second)
        process_check_result(first)

        incident = Incident.objects.get()
        assert incident.failure_count == 2
        # Lower id is older, so it supplies the initial metadata.
        assert incident.failure_type == ErrorType.DNS_ERROR
        assert incident.initial_error_message == 'failure 0'
        assert incident.latest_error_message == 'failure 1'
