from django.db import models

from monitors.models import ErrorType


class IncidentStatus(models.TextChoices):
    OPEN = 'OPEN', 'Open'
    RESOLVED = 'RESOLVED', 'Resolved'


class Incident(models.Model):
    """A confirmed outage for one monitor.

    Ownership is not stored here. It travels incident -> monitor -> website ->
    owner, so there stays exactly one place a row's owner comes from.

    A resolved incident is a historical record: it keeps the failure details it
    was opened with, and nothing reopens it. A later outage creates a new row.
    """

    monitor = models.ForeignKey(
        'monitors.Monitor',
        on_delete=models.CASCADE,
        related_name='incidents',
    )
    status = models.CharField(
        max_length=16,
        choices=IncidentStatus.choices,
        default=IncidentStatus.OPEN,
    )
    # The first failure of the confirmed sequence, not the one that tipped the
    # threshold, so downtime is not under-reported by one interval.
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    # The originating cause. Deliberately stable for the life of the incident:
    # it is what the outage is, while the latest_* fields track how it is
    # currently presenting.
    failure_type = models.CharField(max_length=32, choices=ErrorType.choices)
    initial_error_message = models.CharField(max_length=500, null=True, blank=True)
    latest_error_message = models.CharField(max_length=500, null=True, blank=True)
    initial_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    latest_status_code = models.PositiveSmallIntegerField(null=True, blank=True)

    failure_count = models.PositiveIntegerField(default=0)
    # Progress towards recovery. Reset to zero by any failure, which is what
    # makes the successes it counts consecutive.
    recovery_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-started_at', '-id')
        constraints = [
            # The core invariant, enforced by the database rather than trusted to
            # application code: a monitor can have at most one open incident.
            models.UniqueConstraint(
                fields=['monitor'],
                condition=models.Q(status=IncidentStatus.OPEN),
                name='unique_open_incident_per_monitor',
            )
        ]
        indexes = [
            models.Index(fields=['monitor', '-started_at'], name='incident_monitor_started'),
            models.Index(fields=['status', '-started_at'], name='incident_status_started'),
        ]

    def __str__(self):
        started = f'{self.started_at:%Y-%m-%d %H:%M}'
        return f'{self.status} incident on monitor {self.monitor_id} from {started}'

    @property
    def is_open(self):
        return self.status == IncidentStatus.OPEN
