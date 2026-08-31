"""Notification events and delivery.

Plain functions, no Celery import, so all of this is testable without a broker.

Two halves with a durable boundary between them, the same shape as the rest of
Uptora:

    record_incident_transitions()  decides that somebody should be told, and
                                   commits that decision with the incident
    send_delivery()                talks to an email provider and can be retried

The first half runs inside the incident engine's transaction, so a transition
that rolls back takes its notification with it. The second half never runs
inside that transaction: the provider call happens long after commit, from a
worker holding a lease.
"""

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from incidents.models import IncidentStatus
from notifications.email import (
    PermanentSendError,
    SendError,
    get_email_provider,
)
from notifications.email.rendering import render
from notifications.models import (
    TERMINAL_DELIVERY_STATUSES,
    Channel,
    DeliveryStatus,
    EventType,
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
)

logger = logging.getLogger(__name__)

# Which incident status is worth an email, and what to call it.
EVENT_FOR_STATUS = {
    IncidentStatus.OPEN: EventType.INCIDENT_OPENED,
    IncidentStatus.RESOLVED: EventType.INCIDENT_RESOLVED,
}

# Bounded, widening retries. Four attempts over roughly an hour and a quarter:
# long enough to ride out a provider wobble, short enough that a genuinely
# broken configuration reaches FAILED while somebody still cares.
RETRY_BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
MAX_ATTEMPTS = len(RETRY_BACKOFF)

# Generous next to a 15-second provider timeout, so a slow send is never
# mistaken for a dead worker.
LEASE_DURATION = timedelta(minutes=5)

DISPATCH_BATCH_SIZE = 100
ENQUEUE_DEBOUNCE = timedelta(minutes=2)

ERROR_LIMIT = 500

# Long enough to be useful, short enough to sit in an email without wrapping.
SUMMARY_LIMIT = 300


# Recipients -----------------------------------------------------------


def resolve_recipient(user):
    """Where this user's alerts go, or None if they do not want any.

    A missing preference row means the defaults, so a user who has never opened
    their settings still gets told when their site goes down.

    The address comes only from the account or the user's own preference. There
    is deliberately no path by which a monitored website, a form field or any
    other piece of customer-controlled data can influence it -- otherwise a
    monitor would be a way to make Uptora send mail to a stranger.
    """
    preference = NotificationPreference.objects.filter(user=user).first()
    if preference is None:
        return user.email or None
    if not preference.email_enabled:
        return None
    return preference.recipient or None


# Events ---------------------------------------------------------------


def occurred_at_for(incident, event_type):
    if event_type == EventType.INCIDENT_RESOLVED:
        return incident.resolved_at or timezone.now()
    return incident.started_at


def failure_summary_for(incident):
    """A short description of the fault, from already-redacted fields.

    latest_error_message originates in CheckResult.error_message, which the
    checkers cap and which never contains a configured flow value, a submitted
    form field or a traceback. Nothing further is read here, so there is one
    place to reason about rather than two.
    """
    parts = [incident.get_failure_type_display()]
    if incident.latest_status_code:
        parts.append(f'HTTP {incident.latest_status_code}')
    message = incident.latest_error_message or incident.initial_error_message
    if message:
        parts.append(message)
    return ' - '.join(parts)[:SUMMARY_LIMIT]


def evidence_exists(incident):
    """Whether any failure in this outage captured a screenshot.

    Decided once, when the event occurs, so the renderer never has to reach for
    a CheckResult that may since have been pruned.
    """
    return (
        incident.monitor.results.filter(
            is_success=False,
            screenshot__isnull=False,
            checked_at__gte=incident.started_at,
        )
        .exclude(screenshot='')
        .exists()
    )


def snapshot_of(incident, event_type):
    """The immutable facts an event needs to render and identify itself.

    Copied at the moment the transition is confirmed. The incident, the monitor
    and the website may all change or disappear afterwards; the alert reports
    what was true when it was raised.
    """
    website = incident.monitor.website
    return {
        'monitor_ref': incident.monitor_id,
        'incident_started_at': incident.started_at,
        'incident_resolved_at': incident.resolved_at,
        'monitor_type': incident.monitor.monitor_type,
        'website_name': website.name,
        'website_url': website.url,
        'failure_type': incident.failure_type,
        'failure_summary': failure_summary_for(incident),
        'status_code': incident.latest_status_code,
        'failure_count': incident.failure_count,
        'has_evidence': (
            evidence_exists(incident) if event_type == EventType.INCIDENT_OPENED else False
        ),
    }


def record_incident_transitions(transitions):
    """Turn confirmed incident transitions into notification events.

    Called from inside the incident engine's transaction, which is what makes
    this safe under replay and under rollback.

    Replay: incident state is recomputed on every check, but reconcile matches
    incidents by position and keeps their ids stable, so replaying an unchanged
    stream produces the same incident rows with the same statuses. A transition
    only exists where the status actually moved, and the uniqueness rule on
    (incident, event_type) catches anything that slips through.

    Withdrawal: an incident the stream later says never happened is deleted by
    reconcile, and is not in `transitions` at all, so nothing is emitted for it.
    An incident created and withdrawn within one reconciliation cannot occur --
    only rows that existed before a pass are ever deleted by it.

    An incident that appears already resolved -- reachable only by replaying a
    complete history out of order -- emits the resolved event alone. We never
    observed it open, and "problem detected" followed a second later by
    "recovered" is noise rather than news.

    Returns the events it created.
    """
    # The guarantee this milestone rests on: an event may only be created
    # inside the transaction that is committing the incident transition, so a
    # rollback takes the notification with it. Asserted rather than assumed,
    # because a future caller invoking this outside a transaction would
    # silently be able to announce an outage that never committed.
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('record_incident_transitions must run inside the incident transaction')

    created_events = []

    for incident, previous_status in transitions:
        if incident.status == previous_status:
            continue
        event_type = EVENT_FOR_STATUS.get(incident.status)
        if event_type is None:
            continue

        # Identity is the outage, not the incident row. started_at is derived
        # deterministically from the append-only result stream, so an outage
        # withdrawn and later reconstructed under a new incident id is still
        # recognised as one we have already announced.
        event, created = NotificationEvent.objects.get_or_create(
            monitor_ref=incident.monitor_id,
            event_type=event_type,
            incident_started_at=incident.started_at,
            defaults={
                'incident': incident,
                'occurred_at': occurred_at_for(incident, event_type),
                **snapshot_of(incident, event_type),
            },
        )
        if not created:
            # Restore the convenience link if the previous incident row was
            # withdrawn. The snapshot is left exactly as it was announced.
            if event.incident_id is None:
                NotificationEvent.objects.filter(pk=event.pk).update(incident=incident)
            continue

        logger.info(
            'notification event %s recorded: %s for incident %s',
            event.id,
            event.event_type,
            incident.id,
        )
        create_deliveries(event, incident.monitor.website.owner)
        created_events.append(event)

    return created_events


def create_deliveries(event, owner):
    """Create the deliveries an event needs, and queue them after commit.

    The recipient is snapshotted now rather than looked up at send time, so a
    retry an hour later goes where the alert was addressed and a preference
    change in between cannot redirect an already-created delivery.
    """
    recipient = resolve_recipient(owner)
    if not recipient:
        logger.info('notification event %s has no recipient; nothing to deliver', event.id)
        return []

    delivery, created = NotificationDelivery.objects.get_or_create(
        event=event,
        channel=Channel.EMAIL,
        recipient=recipient,
        defaults={'status': DeliveryStatus.PENDING},
    )
    if created:
        # After commit: a broker message must never refer to a delivery that a
        # rolled-back transaction means does not exist.
        transaction.on_commit(lambda: enqueue_delivery(delivery.id))
    return [delivery]


def enqueue_delivery(delivery_id):
    """Hand a delivery to a worker. Imported late to keep Celery out of here."""
    from notifications.tasks import queue_delivery

    queue_delivery(delivery_id)


# Delivery state machine ------------------------------------------------


def claim_delivery(delivery_id, now=None):
    """Take ownership of a delivery, or decline.

    Returns (token, delivery). A token of None means this delivery must do
    nothing: it is already sent, permanently failed, not yet due for its retry,
    or somebody else is sending it right now.
    """
    now = now or timezone.now()

    with transaction.atomic():
        delivery = NotificationDelivery.objects.select_for_update().get(pk=delivery_id)

        if delivery.status in TERMINAL_DELIVERY_STATUSES:
            logger.info('delivery %s already %s, ignoring', delivery.id, delivery.status)
            return None, delivery

        if delivery.lease_is_live(now):
            logger.info('delivery %s is leased, standing down', delivery.id)
            return None, delivery

        if delivery.next_attempt_at is not None and delivery.next_attempt_at > now:
            logger.info('delivery %s not due until %s', delivery.id, delivery.next_attempt_at)
            return None, delivery

        token = uuid.uuid4()
        delivery.status = DeliveryStatus.SENDING
        delivery.claim_token = token
        delivery.lease_expires_at = now + LEASE_DURATION
        delivery.attempt_count += 1
        delivery.save(
            update_fields=[
                'status',
                'claim_token',
                'lease_expires_at',
                'attempt_count',
                'updated_at',
            ]
        )
        return token, delivery


def owned(delivery_id, token):
    return NotificationDelivery.objects.filter(pk=delivery_id, claim_token=token)


def mark_sent(delivery, token, result):
    owned(delivery.id, token).update(
        status=DeliveryStatus.SENT,
        sent_at=timezone.now(),
        provider_message_id=(result.message_id or '')[:255] or None,
        lease_expires_at=None,
        next_attempt_at=None,
        last_error=None,
    )
    logger.info(
        'delivery %s sent for event %s (provider id %s)',
        delivery.id,
        delivery.event_id,
        result.message_id,
    )


def mark_failed(delivery, token, error):
    owned(delivery.id, token).update(
        status=DeliveryStatus.FAILED,
        lease_expires_at=None,
        next_attempt_at=None,
        last_error=str(error)[:ERROR_LIMIT],
    )
    logger.error('delivery %s failed permanently: %s', delivery.id, str(error)[:200])


def schedule_retry(delivery, token, error, now=None):
    """Back off and try again, or give up once the attempts are spent.

    A failed delivery never touches the incident. An email that could not be
    sent says nothing about whether the customer's site is up.
    """
    now = now or timezone.now()
    if delivery.attempt_count >= MAX_ATTEMPTS:
        mark_failed(delivery, token, f'giving up after {delivery.attempt_count} attempts: {error}')
        return None

    delay = RETRY_BACKOFF[min(delivery.attempt_count, len(RETRY_BACKOFF)) - 1]
    next_attempt = now + delay
    owned(delivery.id, token).update(
        status=DeliveryStatus.PENDING,
        lease_expires_at=None,
        next_attempt_at=next_attempt,
        enqueued_at=None,
        last_error=str(error)[:ERROR_LIMIT],
    )
    logger.warning(
        'delivery %s attempt %d failed, retrying at %s',
        delivery.id,
        delivery.attempt_count,
        next_attempt,
    )
    return next_attempt


def send_delivery(delivery_id, provider=None, now=None):
    """Send one delivery, once.

    Crash boundaries, honestly:

      A. died before the provider call -- the lease expires, the delivery is
         reclaimed and sent. Nothing was sent twice.
      B. the provider accepted the message and the process died before SENT was
         committed -- a reclaim sends it again. An Idempotency-Key derived from
         the delivery id is supplied with every send, so where the provider
         honours it the duplicate is absorbed on their side. Where it is not
         honoured, the customer gets the same alert twice. This is not
         exactly-once and is not claimed to be; the window is one call wide.
      C. died after SENT committed -- a redelivery sees SENT and stops.
    """
    token, delivery = claim_delivery(delivery_id, now=now)
    if token is None:
        return delivery

    provider = provider or get_email_provider()
    event = NotificationEvent.objects.select_related('incident__monitor__website__owner').get(
        pk=delivery.event_id
    )
    subject, text_body, html_body = render(event)

    logger.info(
        'delivery %s attempt %d: sending %s for event %s',
        delivery.id,
        delivery.attempt_count,
        event.event_type,
        event.id,
    )

    try:
        result = provider.send(
            to=delivery.recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            # Stable across every retry of this delivery, which is what lets a
            # provider that supports it collapse a duplicate send.
            idempotency_key=str(delivery.id),
        )
    except PermanentSendError as exc:
        mark_failed(delivery, token, exc)
    except (SendError, Exception) as exc:  # noqa: BLE001 - a bad provider must not kill the worker
        schedule_retry(delivery, token, exc, now=now)
    else:
        mark_sent(delivery, token, result)

    delivery.refresh_from_db()
    return delivery


# Dispatcher ------------------------------------------------------------


@transaction.atomic
def claim_due_deliveries(limit=DISPATCH_BATCH_SIZE, now=None):
    """Find deliveries that need a worker and hand them out.

    The safety net. Deliveries are normally enqueued the moment their event is
    committed, but a broker that is down at that moment would otherwise lose the
    email for good. This scan picks up anything still waiting:

      * PENDING and due -- either never enqueued, or backing off after a failure;
      * SENDING with an expired lease -- the worker holding it died.

    Rows are locked with skip_locked so concurrent dispatchers see disjoint
    sets, and an enqueued_at stamp keeps successive passes from re-sending while
    a message is still queued.
    """
    now = now or timezone.now()
    quiet_since = now - ENQUEUE_DEBOUNCE

    from django.db.models import Q

    due = list(
        NotificationDelivery.objects.select_for_update(skip_locked=True)
        .filter(
            Q(enqueued_at__isnull=True) | Q(enqueued_at__lt=quiet_since),
            Q(
                status=DeliveryStatus.PENDING,
                next_attempt_at__isnull=True,
            )
            | Q(status=DeliveryStatus.PENDING, next_attempt_at__lte=now)
            | Q(status=DeliveryStatus.SENDING, lease_expires_at__lt=now),
        )
        .order_by('created_at')[:limit]
    )
    if not due:
        return []

    NotificationDelivery.objects.filter(pk__in=[d.pk for d in due]).update(enqueued_at=now)
    logger.info('notification dispatcher claimed %d delivery(ies)', len(due))
    return due
