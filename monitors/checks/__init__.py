"""Check implementations, one module per monitor type.

Each returns a CheckOutcome and touches no database, so the same call works from
a view, a management command or a Celery task. Adding a monitor type means
adding a module here and one branch in run_check.
"""

from monitors.checks.base import CheckOutcome, summarize
from monitors.checks.browser import run_browser_check
from monitors.checks.http import run_http_check
from monitors.models import MonitorType

__all__ = [
    'CheckOutcome',
    'run_browser_check',
    'run_check',
    'run_http_check',
    'summarize',
]


class UnsupportedMonitorType(Exception):
    """No check implementation is registered for this monitor type."""


def run_check(monitor, now, transport=None, session_factory=None):
    """Run the check for `monitor` and return a CheckOutcome.

    Does not touch the database; persistence is the caller's job. `transport`
    and `session_factory` are the per-type test seams and are unset in
    production.
    """
    if monitor.monitor_type == MonitorType.HTTP:
        return run_http_check(
            url=monitor.website.url,
            timeout_seconds=monitor.timeout_seconds,
            now=now,
            transport=transport,
        )

    if monitor.monitor_type == MonitorType.BROWSER:
        return run_browser_check(
            url=monitor.website.url,
            timeout_seconds=monitor.timeout_seconds,
            now=now,
            # Read only here: an HTTP monitor never consults these, so leaving
            # them populated on one changes nothing.
            expected_text=monitor.expected_text,
            expected_selector=monitor.expected_selector,
            session_factory=session_factory,
        )

    raise UnsupportedMonitorType(f'No check implemented for {monitor.monitor_type!r}.')
