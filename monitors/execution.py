"""The monitor runner.

execute_monitor is the whole public surface of the monitoring engine. It takes a
model instance and returns a saved CheckResult, with no dependency on the web
layer, so the future Celery task is:

    @shared_task
    def run_monitor(monitor_id):
        execute_monitor(Monitor.objects.get(pk=monitor_id))
"""

from django.utils import timezone

from monitors.checks import CheckOutcome, run_check, summarize
from monitors.models import CheckResult, ErrorType


def execute_monitor(monitor, transport=None):
    """Run one monitor and persist exactly one CheckResult.

    Every path through this function writes a row. A target that is broken,
    hostile or unreachable still produces a record, because "no data" and "the
    site is down" must never look the same in the history.

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
