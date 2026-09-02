import secrets
import uuid

from django.db import models
from django.utils import timezone

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


def incident_share_token():
    """Return 256 bits of URL-safe entropy without predictable row material."""
    return secrets.token_urlsafe(32)


class IncidentShare(models.Model):
    """A revocable public capability for one client-facing incident report.

    The safe snapshot keeps the factual report useful if old CheckResults are
    later pruned. Screenshot bytes remain in the existing evidence store and
    are intentionally not duplicated into this row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name='shares')
    token = models.CharField(
        max_length=64,
        unique=True,
        default=incident_share_token,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    include_evidence = models.BooleanField(default=True)
    snapshot = models.JSONField(default=dict)
    snapshot_updated_at = models.DateTimeField(default=timezone.now)
    evidence_result = models.ForeignKey(
        'monitors.CheckResult',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='incident_shares',
    )

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['incident'],
                condition=models.Q(revoked_at__isnull=True),
                name='unique_unrevoked_share_per_incident',
            )
        ]

    def __str__(self):
        return f'Incident share {self.pk}'

    def is_available(self, now=None):
        now = now or timezone.now()
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)
