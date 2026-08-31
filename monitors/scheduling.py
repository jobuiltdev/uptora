"""Scheduling and worker execution.

Plain functions, no Celery import. The tasks in monitors/tasks.py are thin
wrappers, so the whole scheduler can be driven and tested without a broker.

The database is the source of truth for what is due. Celery Beat runs exactly
one periodic entry -- the dispatcher -- and never a per-monitor schedule, so
there is no broker state that can drift out of sync with the monitors table.

Two guarantees and one honest limit:

  * At most one run exists per (monitor, scheduled_for). A unique constraint
    enforces it, so racing dispatchers cannot both enqueue the same slot.
  * A run performs the network stage at most once *per successful claim*. Once
    a CheckResult is linked, every later delivery skips straight to incident
    processing.
  * What cannot be guaranteed: if a worker dies after the request has gone out
    but before the observation is committed, nothing recorded that it happened.
    When the lease expires another worker will reclaim the run and make the
    request again. See execute_run.
"""

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from monitors.execution import observe, persist_observation, process_recorded_result
from monitors.models import (
    TERMINAL_RUN_STATUSES,
    Monitor,
    MonitorRun,
    MonitorType,
    RunStatus,
)

logger = logging.getLogger(__name__)

# How many monitors one dispatcher pass claims. Bounded so a backlog is worked
# through over several passes rather than in one enormous transaction.
DISPATCH_BATCH_SIZE = 100

HTTP_QUEUE = 'http'
BROWSER_QUEUE = 'browser'

# A lease has to outlast the slowest legitimate execution, or a healthy worker
# gets its run stolen mid-check. Browser and flow checks spend far longer than
# their timeout on launch, navigation and expectations, hence the multiplier
# and the flat margin on top.
LEASE_TIMEOUT_MULTIPLIER = 4
LEASE_MARGIN = timedelta(seconds=90)

INTERNAL_ERROR_LIMIT = 500

# How many abandoned runs one recovery scan re-enqueues.
RECOVERY_BATCH_SIZE = 100

# How long to leave a re-enqueued run alone before considering it stale again.
# Long enough for a busy worker to get to the message, short enough that a lost
# message is retried promptly.
RECOVERY_DEBOUNCE = timedelta(minutes=2)


def queue_for(monitor_type):
    """Which queue a monitor's work belongs on.

    Browser and flow checks each hold a Chromium process, so they are kept on
    their own queue and the operator caps that worker's concurrency. Queue-level
    control rather than an in-process semaphore, because the limit has to hold
    across every worker on the box, not just within one.
    """
    if monitor_type in (MonitorType.BROWSER, MonitorType.FLOW):
        return BROWSER_QUEUE
    return HTTP_QUEUE


def lease_duration(monitor):
    return timedelta(seconds=monitor.timeout_seconds * LEASE_TIMEOUT_MULTIPLIER) + LEASE_MARGIN


def next_slot_after(scheduled_for, interval_seconds, now):
    """The next due time strictly after `now`.

    This is the coalescing rule. A monitor that was due twenty times while
    Uptora was down does not get twenty runs: the current slot produces one
    run, and the next due time jumps past every slot that was missed in a
    single step. Arithmetic rather than a loop, so being days overdue costs the
    same as being one interval overdue.
    """
    interval = timedelta(seconds=interval_seconds)
    if scheduled_for > now:
        return scheduled_for + interval
    missed = int((now - scheduled_for).total_seconds() // interval_seconds) + 1
    return scheduled_for + interval * missed


@transaction.atomic
def claim_due_monitors(limit=DISPATCH_BATCH_SIZE, now=None):
    """Claim every monitor that is due and create one run for each.

    The whole claim happens in one transaction with row locks taken via
    skip_locked, so a second dispatcher running at the same moment simply sees
    a different set of rows rather than blocking or double-claiming. The unique
    constraint on (monitor, scheduled_for) is the backstop underneath that.

    Nothing is enqueued here. The caller sends the messages once this function
    has returned and its transaction has committed, so a message can only ever
    exist for work that is already durably recorded, and no broker call is made
    with the transaction open.

    Returns the list of (run, queue) pairs it created.
    """
    now = now or timezone.now()

    due = list(
        Monitor.objects.select_for_update(skip_locked=True)
        .filter(is_enabled=True, next_check_at__isnull=False, next_check_at__lte=now)
        .order_by('next_check_at', 'id')[:limit]
    )

    claimed = []
    for monitor in due:
        scheduled_for = monitor.next_check_at
        run, created = MonitorRun.objects.get_or_create(
            monitor=monitor,
            scheduled_for=scheduled_for,
            defaults={'status': RunStatus.PENDING},
        )

        # Written straight to the row rather than through save(), so the
        # model's own scheduling rules do not fire and reset what we just
        # computed.
        Monitor.objects.filter(pk=monitor.pk).update(
            next_check_at=next_slot_after(scheduled_for, monitor.interval_seconds, now),
            last_scheduled_at=now,
        )

        if created:
            claimed.append((run, queue_for(monitor.monitor_type)))

    if claimed:
        logger.info('dispatcher claimed %d monitor(s)', len(claimed))
    return claimed


@transaction.atomic
def recover_stale_runs(limit=RECOVERY_BATCH_SIZE, now=None):
    """Find runs whose worker died holding the lease, and hand them back out.

    Without this, an abandoned run waits for Celery to happen to redeliver its
    original message, which may never occur. The scan re-enqueues the *existing*
    run id: no new run, no new slot, no second observation, and Monitor's
    schedule is left exactly where the original dispatch put it.

    Deciding what to do with a recovered run is not this function's job. The
    ordinary execute task reclaims the expired lease and resumes from whatever
    durable state it finds, which is what keeps the crash semantics identical
    whether a run was recovered or merely redelivered.

    Nothing here touches attempt_count: observing an abandoned row is not an
    attempt. Only a worker actually claiming it is.

    FAILED_INTERNAL is left alone. It is terminal, and reviving it is a
    different decision from rescuing an interrupted one.

    Returns the list of (run, queue) pairs to enqueue.
    """
    now = now or timezone.now()
    quiet_since = now - RECOVERY_DEBOUNCE

    stale = list(
        MonitorRun.objects.select_for_update(skip_locked=True)
        .filter(
            Q(recovery_enqueued_at__isnull=True) | Q(recovery_enqueued_at__lt=quiet_since),
            status=RunStatus.RUNNING,
            lease_expires_at__isnull=False,
            lease_expires_at__lt=now,
        )
        .select_related('monitor')
        .order_by('lease_expires_at')[:limit]
    )
    if not stale:
        return []

    # Locked rows, so a concurrent scan is looking at a different set. The
    # stamp keeps the next pass from re-sending while this message is queued.
    MonitorRun.objects.filter(pk__in=[run.pk for run in stale]).update(recovery_enqueued_at=now)

    logger.warning(
        'recovering %d run(s) with abandoned leases: %s',
        len(stale),
        ', '.join(str(run.id) for run in stale),
    )
    return [(run, queue_for(run.monitor.monitor_type)) for run in stale]


def claim_run(run_id, now=None):
    """Take ownership of a run, or decline.

    Returns (token, run). A token of None means this delivery must do nothing:
    the run is finished, cancelled, or someone else holds a live lease on it.

    Reclaiming an expired lease is deliberate. A worker that died holding one
    would otherwise strand the run forever; the cost is the at-least-once edge
    documented in execute_run.
    """
    now = now or timezone.now()

    with transaction.atomic():
        run = (
            MonitorRun.objects.select_for_update().select_related('monitor__website').get(pk=run_id)
        )

        if run.status in TERMINAL_RUN_STATUSES:
            logger.info('run %s already %s, ignoring delivery', run.id, run.status)
            return None, run

        # Disabled between scheduling and execution: the check must not happen.
        # A run already under way is left alone and allowed to finish.
        if not run.monitor.is_enabled and run.status == RunStatus.PENDING:
            run.status = RunStatus.CANCELLED
            run.finished_at = now
            run.save(update_fields=['status', 'finished_at', 'updated_at'])
            logger.info('run %s cancelled: monitor %s disabled', run.id, run.monitor_id)
            return None, run

        if run.lease_is_live(now):
            logger.info('run %s is leased until %s, standing down', run.id, run.lease_expires_at)
            return None, run

        token = uuid.uuid4()
        run.status = RunStatus.RUNNING
        run.claim_token = token
        run.lease_expires_at = now + lease_duration(run.monitor)
        run.claimed_at = run.claimed_at or now
        run.started_at = run.started_at or now
        run.attempt_count += 1
        run.save(
            update_fields=[
                'status',
                'claim_token',
                'lease_expires_at',
                'claimed_at',
                'started_at',
                'attempt_count',
                'updated_at',
            ]
        )
        return token, run


def owned(run_id, token):
    """The rows this worker is still entitled to write to."""
    return MonitorRun.objects.filter(pk=run_id, claim_token=token)


def release_lease(run_id, token, error=None):
    """Give the run back without finishing it.

    The lease is expired rather than the state being rolled back, so a retry
    can pick the run straight up and resume from wherever it got to -- in
    particular, from an already-recorded CheckResult.
    """
    owned(run_id, token).update(
        lease_expires_at=timezone.now(),
        internal_error=(error or '')[:INTERNAL_ERROR_LIMIT] or None,
    )


def execute_run(run_id, transport=None, session_factory=None, now=None):
    """Perform one scheduled run: observe the target, then update incidents.

    The two stages are separated by a durable boundary, exactly as the manual
    path is. Once a CheckResult is linked to this run, no later delivery will
    make another request -- it resumes at incident processing instead. That is
    what stops a retry caused by a database hiccup from re-probing a customer's
    site or writing a second observation.

    Crash boundaries, honestly:

      A. died before the request      -- lease expires, reclaimed, no harm.
      B. died after the request, before it was written -- nothing recorded that
         it happened, so a reclaim makes the request again. This is the edge
         that cannot be closed: an external side effect and a local commit are
         not one atomic act.
      C. died between writing the result and linking it -- impossible. Both
         happen in one transaction.
      D. died during incident processing -- the result is linked, so the retry
         only reprocesses it.
      E. died after incident processing, before COMPLETED -- the retry
         reprocesses the same result, which is idempotent, then completes.

    Returns the run.
    """
    token, run = claim_run(run_id, now=now)
    if token is None:
        return run

    logger.info(
        'run %s starting: monitor %s, attempt %d', run.id, run.monitor_id, run.attempt_count
    )

    try:
        if run.check_result_id is None:
            # Network first, outside any transaction, then the observation and
            # its linkage commit together. Boundary C cannot happen.
            outcome, started_at = observe(
                run.monitor, transport=transport, session_factory=session_factory
            )
            with transaction.atomic():
                result = persist_observation(run.monitor, outcome, started_at)
                linked = owned(run.id, token).update(check_result=result)
            if not linked:
                # The lease was taken while the request was in flight. The
                # observation is real and is kept; the current owner finishes
                # the run, so this delivery stops here rather than racing it.
                logger.warning('run %s lost its lease mid-check', run.id)
                return MonitorRun.objects.get(pk=run.id)
            run.check_result_id = result.pk

        process_recorded_result(run.check_result_id)

        finished = timezone.now()
        completed = owned(run.id, token).update(
            status=RunStatus.COMPLETED,
            finished_at=finished,
            lease_expires_at=None,
            internal_error=None,
        )
        if completed:
            logger.info('run %s completed: result %s', run.id, run.check_result_id)
    except Exception as exc:
        # Uptora's problem, not the customer's. The lease is released so a
        # retry can resume; nothing here writes a CheckResult, so a broker or
        # database failure never becomes a fake outage.
        logger.exception('run %s failed internally', run.id)
        release_lease(run.id, token, error=f'{type(exc).__name__}: {exc}')
        raise

    run.refresh_from_db()
    return run


def mark_failed_internal(run_id, error):
    """Give up on a run after retries are exhausted.

    Terminal for Uptora's bookkeeping only. No CheckResult is written, so the
    customer's incident history is untouched by our own failure.
    """
    updated = (
        MonitorRun.objects.filter(pk=run_id)
        .exclude(status__in=TERMINAL_RUN_STATUSES)
        .update(
            status=RunStatus.FAILED_INTERNAL,
            finished_at=timezone.now(),
            lease_expires_at=None,
            internal_error=(error or '')[:INTERNAL_ERROR_LIMIT] or None,
        )
    )
    if updated:
        logger.error('run %s marked FAILED_INTERNAL: %s', run_id, error)
    return updated
