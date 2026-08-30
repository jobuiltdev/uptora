"""The monitor runner, split into two stages with a durable boundary between them.

    stage 1  record_check(monitor)          -> touches the network, writes one row
    ---------------------------------------- the CheckResult is committed here
    stage 2  process_recorded_result(id)    -> reads that row, updates incidents

Everything expensive, external and non-repeatable happens in stage 1. Stage 2 is
pure bookkeeping over a row that already exists, so it can be retried freely.

The boundary is enforced by the signatures rather than by convention: stage 2
takes a CheckResult id, not a monitor and not a transport. There is no argument
you could pass it that would make it perform an HTTP request, so a retry cannot
accidentally re-probe the customer's site or record a second observation.

The Celery shape this is built for:

    @shared_task(bind=True, max_retries=5)
    def run_monitor(self, monitor_id):
        result = record_check(Monitor.objects.get(pk=monitor_id))
        try:
            process_recorded_result(result.pk)
        except Exception as exc:
            # Retries the incident stage for THIS result. The check is not redone.
            raise self.retry(exc=exc)

Two kinds of failure are handled deliberately differently. Anything the target
site does is caught and recorded as a failed check. Anything *we* get wrong is
allowed to propagate. See execute_monitor for why.
"""

from django.utils import timezone

from incidents.services import process_check_result
from monitors.checks import CheckOutcome, run_check, summarize
from monitors.models import CheckResult, ErrorType


def record_check(monitor, transport=None):
    """Stage 1. Run the check for `monitor` and persist exactly one CheckResult.

    The only function in the pipeline that touches the network, and the only one
    that creates a CheckResult. Every path through it writes exactly one row: a
    target that is broken, hostile or unreachable still produces a record,
    because "no data" and "the site is down" must never look the same in the
    history.

    `transport` is a test seam handed through to the HTTP check.
    """
    started_at = timezone.now()

    try:
        outcome = run_check(monitor, now=started_at, transport=transport)
    except Exception as exc:  # noqa: BLE001 - a bad target must not kill the runner
        outcome = CheckOutcome.failure(ErrorType.UNKNOWN_ERROR, summarize(exc))

    return CheckResult.objects.create(
        monitor=monitor,
        checked_at=started_at,
        **outcome.as_result_fields(),
    )


def process_recorded_result(check_result_id):
    """Stage 2. Update incident state from an already-recorded result.

    The retry entry point. It takes an id so that retrying is only ever able to
    re-read a stored observation: no monitor, no transport, no way back to the
    network. Calling it repeatedly is safe, because incident state is recomputed
    from the result stream rather than incremented.
    """
    return process_check_result(CheckResult.objects.get(pk=check_result_id))


def execute_monitor(monitor, transport=None):
    """Run both stages for one monitor and return the recorded CheckResult.

    Incident processing runs after the result is committed and is intentionally
    not wrapped in a try/except. A bug in our own code is not a fact about the
    customer's website, and recording it as one would be a lie in the history.
    Swallowing it would be worse still: an incident would silently never open
    and nobody would be alerted, which is the one failure a monitoring product
    cannot afford.

    Letting it surface is also the recoverable choice. Stage 1 has already
    committed, and stage 2 runs in its own transaction, so a failure leaves the
    observation stored and unprocessed. Retrying is process_recorded_result on
    that result's id, which never repeats the request.
    """
    result = record_check(monitor, transport=transport)
    process_recorded_result(result.pk)
    return result
