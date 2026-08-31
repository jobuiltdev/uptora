"""Delivery: recipients, the send state machine, retries and crash boundaries.

The provider is a fake that records what it was asked to send, so "did this
alert go out twice" is a number rather than an inference.
"""

from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from monitors.models import CheckResult, ErrorType
from notifications import services, tasks
from notifications.email.base import PermanentSendError, TransientSendError
from notifications.email.fake import FakeEmailProvider
from notifications.email.resend import ResendEmailProvider
from notifications.models import (
    DeliveryStatus,
    EventType,
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
)
from notifications.services import (
    MAX_ATTEMPTS,
    claim_delivery,
    claim_due_deliveries,
    resolve_recipient,
    send_delivery,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider():
    return FakeEmailProvider()


def open_incident(monitor, base=None):
    """Drive a monitor into a confirmed outage, producing one event."""
    from incidents.services import process_check_result

    base = base or timezone.now() - timedelta(hours=1)
    for offset in (0, 1):
        result = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=offset),
            is_success=False,
            error_type=ErrorType.HTTP_ERROR,
            error_message='HTTP 500',
            status_code=500,
        )
        process_check_result(result)
    return Incident.objects.get()


@pytest.fixture
def delivery(monitor):
    open_incident(monitor)
    return NotificationDelivery.objects.get()


class TestRecipientResolution:
    def test_a_missing_preference_falls_back_to_the_account_address(self, user):
        assert resolve_recipient(user) == user.email

    def test_a_preference_row_defaults_to_the_account_address(self, user):
        NotificationPreference.objects.create(user=user)

        assert resolve_recipient(user) == user.email

    def test_a_custom_alert_email_is_used(self, user):
        NotificationPreference.objects.create(user=user, alert_email='ops@example.com')

        assert resolve_recipient(user) == 'ops@example.com'

    def test_disabling_email_means_no_recipient(self, user):
        NotificationPreference.objects.create(user=user, email_enabled=False)

        assert resolve_recipient(user) is None


class TestDeliveryCreation:
    def test_an_event_creates_exactly_one_delivery(self, monitor):
        open_incident(monitor)

        assert NotificationDelivery.objects.count() == 1

    def test_the_delivery_targets_the_owner(self, monitor, user):
        open_incident(monitor)

        assert NotificationDelivery.objects.get().recipient == user.email

    def test_a_custom_address_is_used(self, monitor, user):
        NotificationPreference.objects.create(user=user, alert_email='ops@example.com')

        open_incident(monitor)

        assert NotificationDelivery.objects.get().recipient == 'ops@example.com'

    def test_disabled_email_creates_no_delivery(self, monitor, user):
        NotificationPreference.objects.create(user=user, email_enabled=False)

        open_incident(monitor)

        # The event is still recorded; only the delivery is suppressed.
        assert NotificationEvent.objects.count() == 1
        assert not NotificationDelivery.objects.exists()

    def test_the_recipient_is_snapshotted(self, monitor, user, provider):
        """A preference change must not redirect an alert already addressed."""
        open_incident(monitor)
        original = NotificationDelivery.objects.get().recipient

        NotificationPreference.objects.create(user=user, alert_email='elsewhere@example.com')
        send_delivery(NotificationDelivery.objects.get().id, provider=provider)

        assert provider.last['to'] == original
        assert original != 'elsewhere@example.com'

    def test_a_delivery_starts_pending(self, delivery):
        assert delivery.status == DeliveryStatus.PENDING
        assert delivery.attempt_count == 0


class TestSending:
    def test_a_delivery_is_sent_once(self, delivery, provider):
        sent = send_delivery(delivery.id, provider=provider)

        assert sent.status == DeliveryStatus.SENT
        assert provider.calls == 1
        assert sent.sent_at is not None
        assert sent.provider_message_id

    def test_the_attempt_is_counted(self, delivery, provider):
        sent = send_delivery(delivery.id, provider=provider)

        assert sent.attempt_count == 1

    def test_the_lease_is_released_on_success(self, delivery, provider):
        sent = send_delivery(delivery.id, provider=provider)

        assert sent.lease_expires_at is None

    def test_an_idempotency_key_is_supplied(self, delivery, provider):
        """Stable across retries, so a provider that honours it can dedupe."""
        send_delivery(delivery.id, provider=provider)

        assert provider.last['idempotency_key'] == str(delivery.id)

    def test_the_outage_subject_is_used(self, delivery, provider):
        send_delivery(delivery.id, provider=provider)

        assert provider.last['subject'].startswith('[Uptora] Problem detected on')


class TestDuplicateDelivery:
    def test_a_second_task_delivery_sends_nothing(self, delivery, provider):
        send_delivery(delivery.id, provider=provider)

        send_delivery(delivery.id, provider=provider)

        assert provider.calls == 1

    def test_a_sent_delivery_is_a_no_op(self, delivery, provider):
        send_delivery(delivery.id, provider=provider)
        before = NotificationDelivery.objects.get(pk=delivery.id).sent_at

        again = send_delivery(delivery.id, provider=provider)

        assert again.sent_at == before
        assert again.attempt_count == 1

    def test_many_deliveries_send_once(self, delivery, provider):
        for _ in range(5):
            send_delivery(delivery.id, provider=provider)

        assert provider.calls == 1

    def test_a_live_lease_cannot_be_stolen(self, delivery, provider):
        token, _ = claim_delivery(delivery.id)
        assert token is not None

        send_delivery(delivery.id, provider=provider)

        assert provider.calls == 0

    def test_an_expired_lease_may_be_reclaimed(self, delivery, provider):
        claim_delivery(delivery.id)
        NotificationDelivery.objects.filter(pk=delivery.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        sent = send_delivery(delivery.id, provider=provider)

        assert sent.status == DeliveryStatus.SENT
        assert provider.calls == 1
        assert sent.attempt_count == 2


class TestRetries:
    def test_a_transient_failure_schedules_a_retry(self, delivery):
        flaky = FakeEmailProvider(error=TransientSendError('502 Bad Gateway'))

        result = send_delivery(delivery.id, provider=flaky)

        assert result.status == DeliveryStatus.PENDING
        assert result.next_attempt_at > timezone.now()
        assert '502' in result.last_error

    def test_a_retry_reuses_the_same_delivery(self, delivery):
        flaky = FakeEmailProvider(error=TransientSendError('boom'))
        send_delivery(delivery.id, provider=flaky)

        NotificationDelivery.objects.filter(pk=delivery.id).update(next_attempt_at=None)
        send_delivery(delivery.id, provider=FakeEmailProvider())

        assert NotificationDelivery.objects.count() == 1

    def test_a_delivery_is_not_sent_before_its_retry_is_due(self, delivery, provider):
        send_delivery(delivery.id, provider=FakeEmailProvider(error=TransientSendError('boom')))

        send_delivery(delivery.id, provider=provider)

        assert provider.calls == 0

    def test_backoff_widens(self, delivery):
        flaky = FakeEmailProvider(error=TransientSendError('boom'))
        delays = []
        for _ in range(3):
            before = timezone.now()
            send_delivery(delivery.id, provider=flaky)
            stored = NotificationDelivery.objects.get(pk=delivery.id)
            delays.append((stored.next_attempt_at - before).total_seconds())
            NotificationDelivery.objects.filter(pk=delivery.id).update(next_attempt_at=None)

        assert delays == sorted(delays)
        assert delays[0] < delays[-1]

    def test_retries_are_bounded(self, delivery):
        flaky = FakeEmailProvider(error=TransientSendError('boom'))

        for _ in range(MAX_ATTEMPTS + 2):
            send_delivery(delivery.id, provider=flaky)
            NotificationDelivery.objects.filter(pk=delivery.id).update(next_attempt_at=None)

        stored = NotificationDelivery.objects.get(pk=delivery.id)
        assert stored.status == DeliveryStatus.FAILED
        assert stored.attempt_count == MAX_ATTEMPTS

    def test_a_permanent_failure_does_not_retry(self, delivery):
        rejected = FakeEmailProvider(error=PermanentSendError('422 invalid recipient'))

        result = send_delivery(delivery.id, provider=rejected)

        assert result.status == DeliveryStatus.FAILED
        assert result.attempt_count == 1
        assert result.next_attempt_at is None

    def test_a_failed_delivery_is_terminal(self, delivery, provider):
        send_delivery(delivery.id, provider=FakeEmailProvider(error=PermanentSendError('nope')))

        send_delivery(delivery.id, provider=provider)

        assert provider.calls == 0

    def test_the_error_is_capped(self, delivery):
        noisy = FakeEmailProvider(error=TransientSendError('x' * 5000))

        result = send_delivery(delivery.id, provider=noisy)

        assert len(result.last_error) <= 500


class TestFailureIsolation:
    """A broken mailbox says nothing about the customer's website."""

    def test_a_failed_delivery_leaves_the_incident_open(self, monitor):
        incident = open_incident(monitor)
        delivery = NotificationDelivery.objects.get()

        send_delivery(delivery.id, provider=FakeEmailProvider(error=PermanentSendError('nope')))

        incident.refresh_from_db()
        assert incident.status == IncidentStatus.OPEN

    def test_a_failed_delivery_does_not_touch_check_results(self, monitor):
        open_incident(monitor)
        before = CheckResult.objects.count()

        send_delivery(
            NotificationDelivery.objects.get().id,
            provider=FakeEmailProvider(error=PermanentSendError('nope')),
        )

        assert CheckResult.objects.count() == before

    def test_an_unexpected_provider_error_is_retried_not_raised(self, delivery):
        broken = FakeEmailProvider(error=RuntimeError('provider library exploded'))

        result = send_delivery(delivery.id, provider=broken)

        assert result.status == DeliveryStatus.PENDING


class TestCrashBoundaries:
    def test_a_dying_before_the_provider_call_sends_nothing(self, delivery, provider):
        """A. claimed, then died. Reclaim sends it, once."""
        claim_delivery(delivery.id)
        assert provider.calls == 0

        NotificationDelivery.objects.filter(pk=delivery.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        sent = send_delivery(delivery.id, provider=provider)

        assert sent.status == DeliveryStatus.SENT
        assert provider.calls == 1

    def test_b_accepted_then_died_before_commit_sends_twice(self, delivery):
        """B. the honest edge: the provider took it, we never recorded that.

        A reclaim sends again. The idempotency key gives a provider that
        supports it the chance to collapse the duplicate, but nothing local can
        prevent the second call, and this test says so rather than implying
        otherwise.
        """
        provider = FakeEmailProvider()

        class DiesAfterAccepting(FakeEmailProvider):
            def send(self, **kwargs):
                provider.send(**kwargs)
                raise SystemExit('worker died holding the receipt')

        with pytest.raises(SystemExit):
            send_delivery(delivery.id, provider=DiesAfterAccepting())

        assert provider.calls == 1
        NotificationDelivery.objects.filter(pk=delivery.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        send_delivery(delivery.id, provider=provider)

        # Two provider calls, one email if the provider honours the key.
        assert provider.calls == 2

    def test_c_a_redelivery_after_commit_stops(self, delivery, provider):
        send_delivery(delivery.id, provider=provider)

        send_delivery(delivery.id, provider=provider)

        assert provider.calls == 1


class TestDispatcher:
    def test_a_pending_delivery_is_claimed(self, delivery):
        due = claim_due_deliveries()

        assert [d.id for d in due] == [delivery.id]

    def test_a_sent_delivery_is_ignored(self, delivery, provider):
        send_delivery(delivery.id, provider=provider)

        assert claim_due_deliveries() == []

    def test_a_failed_delivery_is_ignored(self, delivery):
        send_delivery(delivery.id, provider=FakeEmailProvider(error=PermanentSendError('no')))

        assert claim_due_deliveries() == []

    def test_a_delivery_waiting_for_its_retry_is_ignored(self, delivery):
        send_delivery(delivery.id, provider=FakeEmailProvider(error=TransientSendError('boom')))

        assert claim_due_deliveries() == []

    def test_a_due_retry_is_claimed(self, delivery):
        send_delivery(delivery.id, provider=FakeEmailProvider(error=TransientSendError('boom')))
        stored = NotificationDelivery.objects.get(pk=delivery.id)

        due = claim_due_deliveries(now=stored.next_attempt_at + timedelta(seconds=1))

        assert [d.id for d in due] == [delivery.id]

    def test_an_abandoned_sending_delivery_is_claimed(self, delivery):
        claim_delivery(delivery.id)
        NotificationDelivery.objects.filter(pk=delivery.id).update(
            lease_expires_at=timezone.now() - timedelta(minutes=1)
        )

        assert [d.id for d in claim_due_deliveries()] == [delivery.id]

    def test_a_live_sending_delivery_is_ignored(self, delivery):
        claim_delivery(delivery.id)

        assert claim_due_deliveries() == []

    def test_a_second_scan_is_debounced(self, delivery):
        first = claim_due_deliveries()
        second = claim_due_deliveries()

        assert len(first) == 1
        assert second == []

    def test_the_scan_creates_no_extra_deliveries(self, delivery):
        for _ in range(4):
            claim_due_deliveries()

        assert NotificationDelivery.objects.count() == 1


class TestEnqueueTiming:
    def test_the_enqueue_waits_for_the_commit(
        self, monitor, monkeypatch, django_capture_on_commit_callbacks
    ):
        """A broker message must never name a delivery a rollback would erase."""
        sent = []
        monkeypatch.setattr(services, 'enqueue_delivery', lambda did: sent.append(did))

        with django_capture_on_commit_callbacks(execute=True):
            open_incident(monitor)
            # Still inside the transaction: nothing has been queued yet.
            assert sent == []

        assert sent == [NotificationDelivery.objects.get().id]


class TestBrokerOutage:
    def test_a_broker_outage_at_event_time_does_not_lose_the_email(
        self, monitor, monkeypatch, django_capture_on_commit_callbacks
    ):
        """Redis unavailable when the event commits must not lose the alert."""

        def broker_down(delivery_id):
            raise ConnectionError('Error 111 connecting to localhost:6379')

        monkeypatch.setattr(services, 'enqueue_delivery', broker_down)

        with pytest.raises(ConnectionError), django_capture_on_commit_callbacks(execute=True):
            open_incident(monitor)

        # The incident and its alert are committed; only the message was lost.
        assert NotificationEvent.objects.count() == 1
        delivery = NotificationDelivery.objects.get()
        assert delivery.status == DeliveryStatus.PENDING

    def test_the_safety_net_picks_up_an_unqueued_delivery(self, monitor, monkeypatch):
        """No on_commit hook runs here, standing in for a message that was lost."""
        monkeypatch.setattr(services, 'enqueue_delivery', lambda did: None)
        open_incident(monitor)

        due = claim_due_deliveries()

        assert [d.id for d in due] == [NotificationDelivery.objects.get().id]

    def test_a_recovered_broker_then_sends_it(self, monitor, monkeypatch, provider):
        monkeypatch.setattr(services, 'enqueue_delivery', lambda did: None)
        open_incident(monitor)

        ((delivery),) = claim_due_deliveries()
        sent = send_delivery(delivery.id, provider=provider)

        assert sent.status == DeliveryStatus.SENT
        assert provider.calls == 1

    def test_one_failed_enqueue_does_not_abort_the_pass(self, monitor, monkeypatch):
        open_incident(monitor)
        attempts = {'n': 0}

        def flaky(delivery_id):
            attempts['n'] += 1
            raise ConnectionError('broker down')

        monkeypatch.setattr(tasks, 'queue_delivery', flaky)

        result = tasks.dispatch_pending_notifications()

        assert result == {'enqueued': 0}
        assert attempts['n'] == 1
        assert NotificationDelivery.objects.get().status == DeliveryStatus.PENDING


class TestResendProvider:
    """The request-building code, driven without a network."""

    def build(self, handler):
        return ResendEmailProvider(
            api_key='test-key',
            from_address='Uptora <alerts@uptora.example>',
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_a_successful_send_returns_the_message_id(self):
        provider = self.build(lambda request: httpx.Response(200, json={'id': 'abc123'}))

        result = provider.send(to='ops@example.com', subject='hi', text_body='body')

        assert result.message_id == 'abc123'

    def test_the_api_key_is_sent_as_a_bearer_token(self):
        seen = {}

        def handler(request):
            seen['auth'] = request.headers.get('authorization')
            seen['idempotency'] = request.headers.get('idempotency-key')
            return httpx.Response(200, json={'id': 'x'})

        self.build(handler).send(
            to='ops@example.com', subject='hi', text_body='body', idempotency_key='key-1'
        )

        assert seen['auth'] == 'Bearer test-key'
        assert seen['idempotency'] == 'key-1'

    @pytest.mark.parametrize('status', [400, 401, 403, 422])
    def test_client_errors_are_permanent(self, status):
        provider = self.build(lambda request: httpx.Response(status, json={'message': 'bad'}))

        with pytest.raises(PermanentSendError):
            provider.send(to='ops@example.com', subject='hi', text_body='body')

    @pytest.mark.parametrize('status', [429, 500, 502, 503])
    def test_server_errors_and_rate_limits_are_transient(self, status):
        provider = self.build(lambda request: httpx.Response(status, json={'message': 'later'}))

        with pytest.raises(TransientSendError):
            provider.send(to='ops@example.com', subject='hi', text_body='body')

    def test_a_network_error_is_transient(self):
        def handler(request):
            raise httpx.ConnectError('connection refused')

        with pytest.raises(TransientSendError):
            self.build(handler).send(to='ops@example.com', subject='hi', text_body='body')

    def test_the_html_body_is_included_when_present(self):
        seen = {}

        def handler(request):
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json={'id': 'x'})

        self.build(handler).send(
            to='ops@example.com', subject='hi', text_body='text', html_body='<p>html</p>'
        )

        assert seen['html'] == '<p>html</p>'
        assert seen['to'] == ['ops@example.com']


class TestTaskWrapper:
    def test_the_task_sends_and_reports_status(self, delivery, settings, provider, monkeypatch):
        monkeypatch.setattr(services, 'get_email_provider', lambda: provider)

        status = tasks.send_notification_delivery(str(delivery.id))

        assert status == DeliveryStatus.SENT
        assert provider.calls == 1

    def test_the_task_carries_only_an_id(
        self, monitor, monkeypatch, django_capture_on_commit_callbacks
    ):
        messages = []
        monkeypatch.setattr(
            tasks.send_notification_delivery,
            'apply_async',
            lambda args=None, queue=None, **kw: messages.append({'args': args, 'queue': queue}),
        )

        with django_capture_on_commit_callbacks(execute=True):
            open_incident(monitor)

        delivery = NotificationDelivery.objects.get()
        assert messages[0]['args'] == [str(delivery.id)]
        assert messages[0]['queue'] == 'notifications'

    def test_the_resolved_event_also_gets_a_delivery(self, monitor, provider):
        from incidents.services import process_check_result

        base = timezone.now() - timedelta(hours=1)
        open_incident(monitor, base=base)
        for offset in (2, 3):
            result = CheckResult.objects.create(
                monitor=monitor,
                checked_at=base + timedelta(minutes=offset),
                is_success=True,
                status_code=200,
            )
            process_check_result(result)

        resolved = NotificationEvent.objects.get(event_type=EventType.INCIDENT_RESOLVED)
        assert resolved.deliveries.count() == 1
