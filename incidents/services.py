"""The incident state machine.

One entry point, process_check_result, which takes an already-persisted
CheckResult and brings the monitor's incident state up to date. It never creates
a CheckResult and never talks to the web layer, so it is callable from a view
today and from a Celery task later.

Incident state is *derived*, not incrementally mutated. Each call replays the
monitor's results in (checked_at, id) order and writes down what that sequence
implies, rather than nudging counters based on the one result that happened to
arrive. That choice is what makes the engine safe for concurrent workers:

  - Order independence. The answer depends only on what is in the table, not on
    which result triggered the recomputation, so processing #2 before #1 lands
    in the same state as #1 before #2. record_check commits the row before
    processing runs, so every persisted result is visible to every later replay.
  - Idempotency for free. Recomputing a pure function of the same rows produces
    the same rows. There is no "have I already counted this?" bookkeeping to get
    wrong.
  - No compensation logic. Incremental counters would need to know how to undo a
    late-arriving result; a fold simply never applies one twice.

The cost is re-reading a bounded slice of history per check. See replay_cursor
for how that slice is kept short.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from monitors.models import CheckResult, ErrorType, Monitor
from notifications.services import record_incident_transitions

logger = logging.getLogger(__name__)

# How many consecutive results it takes to confirm a state change.
#
# A single timeout is usually a network blip, not an outage. Requiring two in a
# row on the way in stops those becoming incidents, and two in a row on the way
# out stops a flapping site being declared recovered between failures.
#
# Central on purpose: making these per-monitor later means reading them off the
# monitor here rather than hunting through the codebase.
FAILURE_THRESHOLD = 2
RECOVERY_THRESHOLD = 2


@dataclass
class IncidentState:
    """What the ordered result stream says an incident looks like.

    A plain description with no database identity, so the fold can be reasoned
    about and tested without touching rows.
    """

    started_at: datetime
    failure_type: str
    initial_error_message: str | None
    initial_status_code: int | None
    latest_error_message: str | None
    latest_status_code: int | None
    failure_count: int
    recovery_count: int = 0
    status: str = IncidentStatus.OPEN
    resolved_at: datetime | None = None


# Every value the fold decides. Reconciliation writes all of them, so a derived
# field can never quietly keep a stale value from an earlier replay.
DERIVED_FIELDS = (
    'started_at',
    'failure_type',
    'initial_error_message',
    'initial_status_code',
    'latest_error_message',
    'latest_status_code',
    'failure_count',
    'recovery_count',
    'status',
    'resolved_at',
)


def state_values(state):
    return {name: getattr(state, name) for name in DERIVED_FIELDS}


def open_state(run):
    """Build the state for an incident confirmed by a run of consecutive failures."""
    first, last = run[0], run[-1]
    return IncidentState(
        started_at=first.checked_at,
        # The originating cause, fixed for the life of the incident.
        failure_type=first.error_type or ErrorType.UNKNOWN_ERROR,
        initial_error_message=first.error_message,
        initial_status_code=first.status_code,
        latest_error_message=last.error_message,
        latest_status_code=last.status_code,
        failure_count=len(run),
    )


def fold(results):
    """Replay results in chronological order into the incidents they imply.

    The whole state machine lives here, as a pure function of an ordered list.
    Nothing else in this module decides when an incident opens or resolves.
    """
    states = []
    current = None
    pending = []

    for result in results:
        if current is None:
            if result.is_success:
                # Breaks the run, so the failures before it were a blip.
                pending = []
                continue
            pending.append(result)
            if len(pending) >= FAILURE_THRESHOLD:
                current = open_state(pending)
                pending = []
            continue

        if result.is_success:
            current.recovery_count += 1
            if current.recovery_count >= RECOVERY_THRESHOLD:
                current.status = IncidentStatus.RESOLVED
                current.resolved_at = result.checked_at
                states.append(current)
                current = None
                pending = []
        else:
            current.failure_count += 1
            # Any failure abandons whatever recovery progress had been made.
            current.recovery_count = 0
            current.latest_error_message = result.error_message
            current.latest_status_code = result.status_code

    if current is not None:
        states.append(current)
    return states


def open_incident_for(monitor_id):
    return Incident.objects.filter(monitor_id=monitor_id, status=IncidentStatus.OPEN).first()


def replay_cursor(monitor_id, incident, check_result):
    """The last checkpoint the stream has settled. Replay starts after it.

    A checkpoint is a success that has already been through this function. Both
    halves matter:

      - a success resets the machine to idle, so the fold can restart there with
        no carried state;
      - "already processed" means everything before it has been folded and
        reconciled, so skipping it cannot lose an incident.

    The checkpoint must also sit strictly before the result being processed and
    before any open incident began, so a late arrival is never hidden behind a
    newer checkpoint. That is what keeps out-of-order processing exact: an old
    result simply widens its own replay until it reaches settled ground.

    In steady state, where results are processed as they arrive, this keeps the
    replay to the current outage or the trailing run of failures. Returns
    (checked_at, id), or None when the whole stream has to be replayed.
    """
    successes = CheckResult.objects.filter(
        Q(checked_at__lt=check_result.checked_at)
        | Q(checked_at=check_result.checked_at, id__lt=check_result.id),
        monitor_id=monitor_id,
        is_success=True,
        incident_processed_at__isnull=False,
    )
    if incident is not None:
        successes = successes.filter(checked_at__lt=incident.started_at)

    last = successes.order_by('-checked_at', '-id').first()
    return (last.checked_at, last.id) if last else None


def results_after(monitor_id, cursor):
    """The monitor's results after `cursor`, in (checked_at, id) order.

    That pair is the canonical ordering for a monitor's stream: checked_at is
    the meaning, id breaks ties so results recorded in the same instant still
    have one defined sequence.
    """
    queryset = CheckResult.objects.filter(monitor_id=monitor_id)
    if cursor is not None:
        checked_at, result_id = cursor
        queryset = queryset.filter(
            Q(checked_at__gt=checked_at) | Q(checked_at=checked_at, id__gt=result_id)
        )
    return list(queryset.order_by('checked_at', 'id'))


def reconcile(monitor_id, states, since):
    """Make the stored incidents match `states`, the fold's verdict.

    Incidents starting at or after `since` are the fold's responsibility;
    anything earlier is settled history and is not touched. Matching by position
    keeps an existing incident's id stable, so a row the API has already handed
    out does not turn into a different one -- and so a notification already sent
    for it is recognisably the same incident on the next replay.

    Returns (incident, previous_status) for every incident that survives, with
    previous_status None for one just created. That is the only place the
    engine says what actually changed, and it is what notifications are built
    from.
    """
    existing = list(
        Incident.objects.filter(monitor_id=monitor_id, started_at__gte=since).order_by(
            'started_at', 'id'
        )
    )

    transitions = []
    for index, state in enumerate(states):
        if index < len(existing):
            incident = existing[index]
            previous_status = incident.status
            for name, value in state_values(state).items():
                setattr(incident, name, value)
            incident.save(update_fields=[*DERIVED_FIELDS, 'updated_at'])
        else:
            incident = Incident.objects.create(monitor_id=monitor_id, **state_values(state))
            previous_status = None
        transitions.append((incident, previous_status))

    # An incident the ordered stream says never happened, e.g. one opened from
    # two failures that a late-arriving success now sits between. Only reachable
    # for rows the fold owns, and never for settled history.
    #
    # These are not in `transitions`, so a withdrawn incident announces nothing.
    # An incident created and withdrawn in the same pass cannot occur: only rows
    # that existed before this pass are ever deleted by it.
    for spurious in existing[len(states) :]:
        spurious.delete()

    return transitions


@transaction.atomic
def process_check_result(check_result):
    """Bring the monitor's incident state up to date after a recorded result.

    Returns the monitor's open incident, or None when it is not currently down.

    Safe to call with results in any order, more than once, or with a result
    that has already been incorporated: the state is recomputed from the stream
    rather than nudged, so every call converges on the same answer.
    """
    # Locking the monitor serializes recomputation for it, so two workers cannot
    # interleave a read of the stream with each other's writes. The partial
    # unique constraint on Incident is the backstop if this is ever bypassed.
    Monitor.objects.select_for_update().get(pk=check_result.monitor_id)

    monitor_id = check_result.monitor_id
    cursor = replay_cursor(monitor_id, open_incident_for(monitor_id), check_result)
    window = results_after(monitor_id, cursor)

    if window:
        transitions = reconcile(monitor_id, fold(window), since=window[0].checked_at)
        for incident, previous_status in transitions:
            if previous_status is None:
                logger.warning(
                    'incident opened: incident=%s monitor=%s failure_type=%s',
                    incident.id,
                    monitor_id,
                    incident.failure_type,
                )
            elif previous_status != incident.status:
                logger.info(
                    'incident resolved: incident=%s monitor=%s failures=%s',
                    incident.id,
                    monitor_id,
                    incident.failure_count,
                )
        # The single integration point for notifications. It runs inside this
        # transaction, so a transition that rolls back takes its notification
        # with it, and it is driven by incident state alone -- a manual run and
        # a scheduled one are indistinguishable from here.
        record_incident_transitions(transitions)

    stamp_processed(check_result)
    return open_incident_for(monitor_id)


def stamp_processed(check_result):
    """Record that this result has been through the incident stage.

    Bookkeeping only. Correctness no longer depends on it, because recomputation
    is idempotent; it exists so an operator can see which results a failed retry
    still owes, and so the retry path can find them.
    """
    CheckResult.objects.filter(pk=check_result.pk, incident_processed_at__isnull=True).update(
        incident_processed_at=timezone.now()
    )
    check_result.refresh_from_db(fields=['incident_processed_at'])
