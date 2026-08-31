"""Notification history must outlive the incidents that prompted it.

An Incident is derived and may be withdrawn when a late observation says the
outage never happened. An email is an irreversible effect on the outside world.
These tests hold the line between the two.
"""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from incidents.models import Incident
from monitors.models import (
    CheckResult,
    ErrorType,
    FlowConfig,
    FlowField,
    FlowFieldType,
    FlowKind,
    Monitor,
    MonitorType,
)
from notifications.email.fake import FakeEmailProvider
from notifications.email.rendering import render
from notifications.models import (
    DeliveryStatus,
    EventType,
    NotificationDelivery,
    NotificationEvent,
)
from notifications.services import (
    record_incident_transitions,
    send_delivery,
)

pytestmark = pytest.mark.django_db

SECRET_VALUE = 'Priya Raman priya@example.com SUPERSECRETTOKEN'


def fail_twice(monitor, base=None, **fields):
    from incidents.services import process_check_result

    base = base or timezone.now() - timedelta(hours=1)
    defaults = {
        'is_success': False,
        'error_type': ErrorType.HTTP_ERROR,
        'error_message': 'HTTP 503 Service Unavailable',
        'status_code': 503,
    }
    defaults.update(fields)
    for offset in (0, 1):
        result = CheckResult.objects.create(
            monitor=monitor, checked_at=base + timedelta(minutes=offset), **defaults
        )
        process_check_result(result)
    return Incident.objects.order_by('-started_at').first()


def succeed_twice(monitor, base):
    from incidents.services import process_check_result

    for offset in (2, 3):
        result = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=offset),
            is_success=True,
            status_code=200,
        )
        process_check_result(result)


@pytest.fixture
def provider():
    return FakeEmailProvider()


class TestSentHistorySurvives:
    """The most important guarantee: an email that went out stays auditable."""

    @pytest.fixture
    def sent(self, monitor, provider):
        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()
        send_delivery(delivery.id, provider=provider)
        return NotificationDelivery.objects.get(pk=delivery.id)

    def test_the_event_survives_incident_deletion(self, sent):
        Incident.objects.all().delete()

        assert NotificationEvent.objects.count() == 1

    def test_the_delivery_survives_incident_deletion(self, sent):
        Incident.objects.all().delete()

        assert NotificationDelivery.objects.count() == 1

    def test_the_provider_receipt_is_preserved(self, sent):
        before = sent.provider_message_id
        Incident.objects.all().delete()

        stored = NotificationDelivery.objects.get()
        assert stored.provider_message_id == before
        assert stored.provider_message_id

    def test_the_send_timestamp_and_status_are_preserved(self, sent):
        Incident.objects.all().delete()

        stored = NotificationDelivery.objects.get()
        assert stored.status == DeliveryStatus.SENT
        assert stored.sent_at == sent.sent_at
        assert stored.attempt_count == 1

    def test_the_link_is_cleared_rather_than_the_row(self, sent):
        Incident.objects.all().delete()

        assert NotificationEvent.objects.get().incident_id is None

    def test_the_audit_record_still_says_what_it_was_about(self, sent, website):
        Incident.objects.all().delete()

        event = NotificationEvent.objects.get()
        assert event.website_name == website.name
        assert event.website_url == website.url
        assert event.event_type == EventType.INCIDENT_OPENED
        assert event.occurred_at is not None

    def test_a_retrying_delivery_survives_incident_deletion(self, monitor):
        from notifications.email.base import TransientSendError

        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()
        send_delivery(delivery.id, provider=FakeEmailProvider(error=TransientSendError('502')))

        Incident.objects.all().delete()

        stored = NotificationDelivery.objects.get(pk=delivery.id)
        assert stored.status == DeliveryStatus.PENDING
        assert stored.attempt_count == 1
        assert stored.next_attempt_at is not None

    def test_a_failed_delivery_survives_incident_deletion(self, monitor):
        from notifications.email.base import PermanentSendError

        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()
        send_delivery(delivery.id, provider=FakeEmailProvider(error=PermanentSendError('bad')))

        Incident.objects.all().delete()

        stored = NotificationDelivery.objects.get(pk=delivery.id)
        assert stored.status == DeliveryStatus.FAILED
        assert stored.last_error


class TestPendingDeliveryWithoutIncident:
    """A queued alert must still be sendable after its incident disappears."""

    def test_a_pending_delivery_still_sends(self, monitor, provider):
        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()
        Incident.objects.all().delete()

        sent = send_delivery(delivery.id, provider=provider)

        assert sent.status == DeliveryStatus.SENT
        assert provider.calls == 1

    def test_the_email_still_renders_from_the_snapshot(self, monitor, provider, website):
        fail_twice(monitor)
        Incident.objects.all().delete()

        send_delivery(NotificationDelivery.objects.get().id, provider=provider)

        assert website.name in provider.last['text_body']
        assert 'HTTP 503' in provider.last['text_body']

    def test_the_subject_is_unaffected(self, monitor, provider):
        fail_twice(monitor)
        Incident.objects.all().delete()

        send_delivery(NotificationDelivery.objects.get().id, provider=provider)

        assert provider.last['subject'] == '[Uptora] Problem detected on example.com'

    def test_rendering_does_not_touch_the_incident(self, monitor):
        fail_twice(monitor)
        event = NotificationEvent.objects.get()
        Incident.objects.all().delete()
        event.refresh_from_db()
        assert event.incident_id is None

        subject, text, html = render(event)

        assert subject and text and html

    def test_a_recovery_delivery_still_renders(self, monitor, provider):
        base = timezone.now() - timedelta(hours=1)
        fail_twice(monitor, base=base)
        succeed_twice(monitor, base)
        resolved = NotificationEvent.objects.get(event_type=EventType.INCIDENT_RESOLVED)
        delivery = resolved.deliveries.get()
        Incident.objects.all().delete()

        sent = send_delivery(delivery.id, provider=provider)

        assert sent.status == DeliveryStatus.SENT
        assert 'has recovered' in provider.last['subject']

    def test_the_recovery_duration_survives(self, monitor, provider):
        base = timezone.now() - timedelta(hours=1)
        fail_twice(monitor, base=base)
        succeed_twice(monitor, base)
        resolved = NotificationEvent.objects.get(event_type=EventType.INCIDENT_RESOLVED)
        Incident.objects.all().delete()
        resolved.refresh_from_db()

        _, text, _ = render(resolved)

        assert 'Duration:  3m 0s' in text
        assert 'Failed checks: 2' in text


class TestSnapshotImmutability:
    """A retry reports the event as it was, not as the world has since become."""

    def test_a_renamed_website_does_not_change_an_existing_event(self, monitor, website, provider):
        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()

        website.name = 'Completely Different Name'
        website.url = 'https://renamed.example.com/'
        website.save()

        send_delivery(delivery.id, provider=provider)

        assert 'Example' in provider.last['text_body']
        assert 'Completely Different Name' not in provider.last['text_body']
        assert 'renamed.example.com' not in provider.last['subject']

    def test_a_changed_incident_does_not_change_an_existing_event(self, monitor, provider):
        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()

        Incident.objects.all().update(
            latest_error_message='something else entirely',
            latest_status_code=418,
            failure_count=99,
        )

        send_delivery(delivery.id, provider=provider)

        assert 'HTTP 503' in provider.last['text_body']
        assert 'something else entirely' not in provider.last['text_body']
        assert '418' not in provider.last['text_body']

    def test_the_snapshot_is_written_once(self, monitor):
        from incidents.services import process_check_result

        base = timezone.now() - timedelta(hours=1)
        fail_twice(monitor, base=base)
        event = NotificationEvent.objects.get()
        original = event.failure_summary

        # More failures update the incident but must not rewrite the event.
        third = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=2),
            is_success=False,
            error_type=ErrorType.TIMEOUT,
            error_message='a totally different failure',
        )
        process_check_result(third)

        event.refresh_from_db()
        assert event.failure_summary == original
        assert 'totally different' not in event.failure_summary


class TestStableOutageIdentity:
    """Identity is the outage, not the incident row."""

    def test_a_re_derived_outage_does_not_realert(self, monitor):
        """The case the incident primary key could not protect against.

        An outage is confirmed, withdrawn by a late success, then reconstructed
        by a later failure landing inside the original run. The incident gets a
        new id; the outage is the same one, and must not be announced twice.
        """
        from incidents.services import process_check_result

        base = timezone.now() - timedelta(hours=1)
        first = CheckResult.objects.create(
            monitor=monitor, checked_at=base, is_success=False, error_message='down'
        )
        third = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=2),
            is_success=False,
            error_message='down',
        )
        process_check_result(first)
        process_check_result(third)
        original_incident = Incident.objects.get()
        assert NotificationEvent.objects.count() == 1

        # A late success withdraws it.
        between = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=1),
            is_success=True,
            status_code=200,
        )
        process_check_result(between)
        assert not Incident.objects.exists()

        # A late failure inside the original run reconstructs it.
        late = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(seconds=30),
            is_success=False,
            error_message='down',
        )
        process_check_result(late)

        rebuilt = Incident.objects.get()
        assert rebuilt.pk != original_incident.pk
        assert rebuilt.started_at == original_incident.started_at
        # Same outage, same identity, no second alert.
        assert NotificationEvent.objects.count() == 1
        assert NotificationDelivery.objects.count() == 1

    def test_the_link_is_restored_to_the_rebuilt_incident(self, monitor):
        from incidents.services import process_check_result

        base = timezone.now() - timedelta(hours=1)
        for offset in (0, timedelta(minutes=2).total_seconds()):
            CheckResult.objects.create(
                monitor=monitor,
                checked_at=base + timedelta(seconds=offset),
                is_success=False,
                error_message='down',
            )
        for result in CheckResult.objects.order_by('checked_at'):
            process_check_result(result)

        between = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=1),
            is_success=True,
            status_code=200,
        )
        process_check_result(between)
        assert NotificationEvent.objects.get().incident_id is None

        late = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(seconds=30),
            is_success=False,
            error_message='down',
        )
        process_check_result(late)

        assert NotificationEvent.objects.get().incident_id == Incident.objects.get().pk

    def test_a_genuinely_later_outage_does_alert(self, monitor):
        """A different start time is a different outage."""
        base = timezone.now() - timedelta(hours=2)
        fail_twice(monitor, base=base)
        succeed_twice(monitor, base)
        fail_twice(monitor, base=base + timedelta(minutes=10))

        assert NotificationEvent.objects.filter(event_type=EventType.INCIDENT_OPENED).count() == 2


class TestTransactionGuarantee:
    """The event may only exist if the incident transition committed."""

    def test_a_rolled_back_transition_leaves_no_event(self, monitor, monkeypatch):
        import incidents.services as services
        from incidents.services import process_check_result

        base = timezone.now() - timedelta(hours=1)
        first = CheckResult.objects.create(
            monitor=monitor, checked_at=base, is_success=False, error_message='down'
        )
        process_check_result(first)

        real_stamp = services.stamp_processed

        def stamp_then_fail(check_result):
            real_stamp(check_result)
            raise RuntimeError('database went away mid-transaction')

        monkeypatch.setattr(services, 'stamp_processed', stamp_then_fail)
        second = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=1),
            is_success=False,
            error_message='down',
        )
        with pytest.raises(RuntimeError):
            process_check_result(second)

        assert not Incident.objects.exists()
        assert not NotificationEvent.objects.exists()
        assert not NotificationDelivery.objects.exists()

    def test_a_committed_transition_creates_exactly_one_event(self, monitor):
        fail_twice(monitor)

        assert NotificationEvent.objects.count() == 1
        assert NotificationDelivery.objects.count() == 1


@pytest.mark.django_db(transaction=True)
class TestRealTransactionRollback:
    """The same guarantee against a real connection, not a nested savepoint."""

    def test_creating_events_outside_a_transaction_is_refused(self):
        """Structural, not merely conventional.

        transaction=True gives a real connection with no ambient atomic block,
        so this exercises the guard rather than a nested savepoint.
        """
        with pytest.raises(RuntimeError, match='inside the incident transaction'):
            record_incident_transitions([])

    def test_an_event_does_not_survive_a_real_rollback(self, django_user_model):
        from websites.models import Website

        user = django_user_model.objects.create_user(
            email='rollback@example.com', password='pass-1234'
        )
        website = Website.objects.create(
            owner=user, name='Rollback', url='https://rollback.example.com/'
        )
        monitor = Monitor.objects.create(website=website)

        base = timezone.now() - timedelta(hours=1)
        from incidents.services import process_check_result

        first = CheckResult.objects.create(
            monitor=monitor, checked_at=base, is_success=False, error_message='down'
        )
        process_check_result(first)

        second = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=1),
            is_success=False,
            error_message='down',
        )
        try:
            with transaction.atomic():
                process_check_result(second)
                raise RuntimeError('rolled back after the transition')
        except RuntimeError:
            pass

        assert not Incident.objects.exists()
        assert not NotificationEvent.objects.exists()

        Website.objects.all().delete()
        user.delete()


class TestSnapshotSecurity:
    """Nothing customer-controlled may reach a snapshot column."""

    @pytest.fixture
    def flow_event(self, website):
        monitor = Monitor.objects.create(website=website, monitor_type=MonitorType.FLOW)
        config = FlowConfig.objects.create(
            monitor=monitor,
            flow_kind=FlowKind.CONTACT_FORM,
            submit_selector='#send',
            success_text='Thanks',
        )
        FlowField.objects.create(
            flow_config=config,
            selector='#name',
            field_type=FlowFieldType.TEXT,
            value=SECRET_VALUE,
        )
        fail_twice(
            monitor,
            error_type=ErrorType.FLOW_SUCCESS_TEXT_MISSING,
            error_message="Submitted, but the success text never appeared: 'Thanks'",
            status_code=200,
        )
        return NotificationEvent.objects.get()

    def test_no_snapshot_column_holds_a_configured_value(self, flow_event):
        for value in flow_event.__dict__.values():
            assert SECRET_VALUE not in str(value)
            assert 'SUPERSECRETTOKEN' not in str(value)

    def test_the_rendered_email_holds_no_configured_value(self, flow_event):
        subject, text, html = render(flow_event)

        for blob in (subject, text, html):
            assert SECRET_VALUE not in blob
            assert 'priya@example.com' not in blob

    def test_the_summary_is_capped(self, monitor):
        """Capped twice over: CheckResult.error_message is itself a
        CharField(500), so an oversized message cannot reach the snapshot in
        the first place, and the summary trims again on top of that."""
        fail_twice(monitor, error_message='y' * 500)

        summary = NotificationEvent.objects.get().failure_summary
        assert len(summary) <= 500

    def test_no_traceback_reaches_the_snapshot(self, monitor):
        """Error messages come from CheckResult, which caps to one line."""
        fail_twice(
            monitor,
            error_message='RuntimeError: boom',
            error_type=ErrorType.UNKNOWN_ERROR,
        )

        event = NotificationEvent.objects.get()
        assert 'Traceback' not in event.failure_summary
        assert '\n' not in event.failure_summary

    def test_no_lease_token_reaches_the_snapshot(self, monitor, provider):
        fail_twice(monitor)
        delivery = NotificationDelivery.objects.get()
        send_delivery(delivery.id, provider=provider)

        event = NotificationEvent.objects.get()
        stored = NotificationDelivery.objects.get(pk=delivery.id)
        assert str(stored.claim_token) not in str(event.__dict__)
