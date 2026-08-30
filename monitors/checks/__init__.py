"""Check implementations, one module per monitor type.

Adding Playwright later means adding a browser module here and one branch in
run_check. Nothing outside this package needs to know how a check is performed.
"""

from monitors.checks.base import CheckOutcome, summarize
from monitors.checks.http import run_http_check
from monitors.models import MonitorType

__all__ = ['CheckOutcome', 'run_check', 'run_http_check', 'summarize']


class UnsupportedMonitorType(Exception):
    """No check implementation is registered for this monitor type."""


def run_check(monitor, now, transport=None):
    """Run the check for `monitor` and return a CheckOutcome.

    Does not touch the database; persistence is the caller's job.
    """
    if monitor.monitor_type == MonitorType.HTTP:
        return run_http_check(
            url=monitor.website.url,
            timeout_seconds=monitor.timeout_seconds,
            now=now,
            transport=transport,
        )
    raise UnsupportedMonitorType(f'No check implemented for {monitor.monitor_type!r}.')
