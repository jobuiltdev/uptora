"""Notification preferences, events and deliveries.

Three separate concerns, deliberately not collapsed:

  NotificationPreference  who wants to hear about it, and where
  NotificationEvent       a confirmed incident transition worth telling someone
  NotificationDelivery    one attempt to actually tell them, on one channel

The split is what makes retries safe. An event is a fact about an incident and
is created once; a delivery is a fallible interaction with an outside system and
carries all the state a retry needs.
"""

import uuid

from django.conf import settings
from django.db import models


class NotificationPreference(models.Model):
    """Where a user wants alerts sent.

    A missing row means the defaults -- email on, sent to the account address --
    so a user who has never touched their settings is still notified. Nothing
    creates these rows eagerly; see resolve_recipient in services.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_preference',
    )
    email_enabled = models.BooleanField(default=True)
    # Optional override. Blank means "use the account address", which stays
    # correct if the user later changes their login email.
    alert_email = models.EmailField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'notification preference for {self.user_id}'

    @property
    def recipient(self):
        return self.alert_email or self.user.email


class EventType(models.TextChoices):
    INCIDENT_OPENED = 'INCIDENT_OPENED', 'Incident opened'
    INCIDENT_RESOLVED = 'INCIDENT_RESOLVED', 'Incident resolved'


class NotificationEvent(models.Model):
    """A confirmed incident transition that somebody was told about.

    Deliberately durable in a way an Incident is not. Incidents are *derived*:
    the engine recomputes them from the observation stream on every check and
    will withdraw one that a late result says never happened. An email is the
    opposite kind of thing -- an irreversible effect on the outside world. Once
    it has been sent, or even queued, the record of it has to outlive whatever
    the incident engine later concludes.

    So this row owns a snapshot of everything needed to render and audit the
    notification, and the link back to the incident is a convenience that may
    become null. Nothing here reads live incident state.

    Identity is (monitor, event_type, incident_started_at) rather than the
    incident's primary key. started_at is the checked_at of the first failure in
    the confirmed run, which the fold derives deterministically from an
    append-only stream -- so the same logical outage keeps the same identity even
    if its incident row is withdrawn and later reconstructed with a new id.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Convenience link only. SET_NULL because a derived incident may be
    # withdrawn, and losing the audit trail for an email already sent would be
    # far worse than losing a foreign key.
    incident = models.ForeignKey(
        'incidents.Incident',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notification_events',
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    # When the thing being reported happened, not when we noticed it.
    occurred_at = models.DateTimeField()

    # --- immutable snapshot -------------------------------------------
    #
    # Enough to render the email and identify the event without touching any
    # mutable row. A retry hours later therefore reports the event as it was
    # when it occurred, not as the world has since become.
    #
    # Plain columns rather than foreign keys on purpose: a FK to Monitor would
    # reintroduce exactly the cascade this model exists to escape.
    #
    # Nothing customer-controlled is copied here. No flow field values, no
    # submitted form contents, no cookies, no tracebacks, no lease tokens. The
    # failure summary is built from CheckResult.error_message, which is already
    # capped and already excludes those.

    # Identity. Null only on rows predating this snapshot; every event created
    # since carries both.
    monitor_ref = models.PositiveIntegerField(null=True, blank=True)
    incident_started_at = models.DateTimeField(null=True, blank=True)

    monitor_type = models.CharField(max_length=16, blank=True, default='')
    website_name = models.CharField(max_length=200, blank=True, default='')
    website_url = models.CharField(max_length=500, blank=True, default='')

    incident_resolved_at = models.DateTimeField(null=True, blank=True)
    failure_type = models.CharField(max_length=32, blank=True, default='')
    # A short, already-redacted description of what went wrong.
    failure_summary = models.CharField(max_length=500, blank=True, default='')
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    failure_count = models.PositiveIntegerField(default=0)
    # Whether a screenshot was captured, decided when the event occurred so the
    # renderer never has to query CheckResult.
    has_evidence = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-occurred_at', '-created_at')
        constraints = [
            models.UniqueConstraint(
                fields=['monitor_ref', 'event_type', 'incident_started_at'],
                name='notificationevent_unique_outage_transition',
            )
        ]
        indexes = [
            models.Index(fields=['event_type', '-occurred_at'], name='notifevent_type_time'),
            models.Index(fields=['monitor_ref', '-occurred_at'], name='notifevent_monitor_time'),
        ]

    def __str__(self):
        return f'{self.event_type} for {self.website_name or self.incident_id}'


class Channel(models.TextChoices):
    EMAIL = 'EMAIL', 'Email'


class DeliveryStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    SENDING = 'SENDING', 'Sending'
    SENT = 'SENT', 'Sent'
    FAILED = 'FAILED', 'Failed'


TERMINAL_DELIVERY_STATUSES = frozenset({DeliveryStatus.SENT, DeliveryStatus.FAILED})


class NotificationDelivery(models.Model):
    """One attempt to deliver one event to one address on one channel.

    The recipient is snapshotted here when the event occurs rather than looked
    up at send time. A retry three hours later then goes to the address the
    alert was addressed to, not wherever the user has since redirected their
    mail, which keeps retries deterministic.

    The body is not stored. It is a pure function of the event and the incident,
    so keeping a copy would only create a second version of the truth that could
    drift -- and would put failure details in a second place to have to redact.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        NotificationEvent,
        on_delete=models.CASCADE,
        related_name='deliveries',
    )
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.EMAIL)
    recipient = models.EmailField()

    status = models.CharField(
        max_length=16,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    provider_message_id = models.CharField(max_length=255, null=True, blank=True)
    # Capped, and never the provider's raw response: an error string is for an
    # operator to read, not a place to accumulate payloads.
    last_error = models.CharField(max_length=500, null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # The lease, mirroring MonitorRun: only the holder of the current token may
    # talk to the provider, so a duplicate task delivery cannot send twice.
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    # Debounce for the safety-net scan, so a queued delivery is not re-sent
    # every time the dispatcher runs.
    enqueued_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'channel', 'recipient'],
                name='notificationdelivery_unique_target',
            )
        ]
        indexes = [
            models.Index(fields=['status', 'next_attempt_at'], name='notifdelivery_status_due'),
        ]

    def __str__(self):
        return f'{self.channel} delivery {self.id} ({self.status})'

    def lease_is_live(self, now):
        return self.lease_expires_at is not None and self.lease_expires_at > now
