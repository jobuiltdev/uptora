"""Celery tasks for notifications.

Thin, like the monitor tasks. Everything worth testing is in
notifications/services.py and needs no broker.

Arguments are ids only. No recipient, no subject, no body and above all no API
key ever goes into a message payload: a broker is readable by anyone with
access to it.
"""

import logging

from celery import shared_task

from notifications.services import claim_due_deliveries, send_delivery

logger = logging.getLogger(__name__)

NOTIFICATIONS_QUEUE = 'notifications'


def queue_delivery(delivery_id):
    send_notification_delivery.apply_async(args=[str(delivery_id)], queue=NOTIFICATIONS_QUEUE)


@shared_task(name='notifications.send_notification_delivery')
def send_notification_delivery(delivery_id):
    """Send one delivery.

    Safe to deliver more than once: send_delivery claims a lease, no-ops on a
    delivery that is already sent or permanently failed, and reschedules its own
    retry rather than relying on Celery's. Retry timing lives in the database so
    it survives a broker restart.
    """
    delivery = send_delivery(delivery_id)
    return delivery.status


@shared_task(name='notifications.dispatch_pending_notifications')
def dispatch_pending_notifications():
    """Safety net for deliveries that never got a worker.

    Covers a broker that was down when the event was recorded, a retry that has
    come due, and a worker that died mid-send.
    """
    claimed = claim_due_deliveries()
    sent = 0
    for delivery in claimed:
        try:
            queue_delivery(delivery.id)
        except Exception:  # noqa: BLE001 - a broker outage must not abort the pass
            logger.exception('could not enqueue delivery %s; it stays recoverable', delivery.id)
            continue
        sent += 1
    return {'enqueued': sent}
