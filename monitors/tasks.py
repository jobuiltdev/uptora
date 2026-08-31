"""Celery tasks.

Deliberately thin. Everything worth testing lives in monitors/scheduling.py and
needs no broker; these wrappers only add delivery, retry policy and routing.

Task arguments are ids and nothing else. No URL, no selector and above all no
configured flow value is ever placed in a message body or a log line -- broker
payloads are readable by anyone with Redis access, and a contact form's fields
may hold a real person's name and address.
"""

import logging

from celery import shared_task

from monitors.scheduling import (
    claim_due_monitors,
    execute_run,
    mark_failed_internal,
    recover_stale_runs,
)

logger = logging.getLogger(__name__)

# Enough attempts to ride out a transient database or broker problem, not so
# many that a genuine bug retries all day.
MAX_EXECUTION_RETRIES = 3
RETRY_BACKOFF_SECONDS = 10


@shared_task(name='monitors.dispatch_due_monitors')
def dispatch_due_monitors():
    """Claim every due monitor, rescue every abandoned run, and enqueue both.

    The single periodic entry in the beat schedule. Both scans complete and
    commit before anything is sent, so a message only ever exists for work that
    is already durably recorded and no broker call is made with a transaction
    open.

    Recovery runs first: a run whose worker died is already late, and it costs
    nothing to prefer it over work that is only just due.
    """
    recovered = enqueue_all(recover_stale_runs())
    dispatched = enqueue_all(claim_due_monitors())
    return {'dispatched': dispatched, 'recovered': recovered}


def enqueue_all(pairs):
    """Send one message per run, surviving a broker that is not there.

    A failed send is logged and skipped rather than raised. The run is left
    exactly as it was -- still RUNNING with an expired lease, or still PENDING
    -- so a later pass picks it up. Above all it writes no CheckResult: Redis
    being down is Uptora's problem and must never be recorded as the customer's
    site failing.
    """
    sent = 0
    for run, queue in pairs:
        try:
            enqueue_run(run.id, queue)
        except Exception:  # noqa: BLE001 - a broker outage must not abort the pass
            logger.exception('could not enqueue run %s; it stays recoverable', run.id)
            continue
        sent += 1
    return sent


def enqueue_run(run_id, queue):
    execute_monitor_run.apply_async(args=[str(run_id)], queue=queue)


@shared_task(bind=True, name='monitors.execute_monitor_run', max_retries=MAX_EXECUTION_RETRIES)
def execute_monitor_run(self, run_id):
    """Execute one scheduled run.

    Safe to deliver more than once: execute_run claims a lease, no-ops when the
    run is finished or held by someone else, and skips the network stage
    entirely once a CheckResult is linked.

    A retry here retries Uptora's bookkeeping, never the customer's website.
    """
    try:
        run = execute_run(run_id)
    except Exception as exc:
        if self.request.retries >= MAX_EXECUTION_RETRIES:
            mark_failed_internal(run_id, f'{type(exc).__name__}: {exc}')
            # Swallowed on purpose: the run is recorded as an internal failure,
            # and re-raising would only add noise to the worker log for a
            # condition already stored and queryable.
            return 'FAILED_INTERNAL'
        raise self.retry(exc=exc, countdown=RETRY_BACKOFF_SECONDS) from exc
    return run.status
