"""Preference API, email rendering, and what must never appear in an alert."""

from datetime import timedelta

import pytest
from django.urls import reverse
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
from notifications.email.rendering import humanize_duration, render
from notifications.models import (
    EventType,
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
)
from notifications.services import send_delivery

pytestmark = pytest.mark.django_db

PREFERENCES_URL = 'notification-preferences'

SECRET_VALUE = 'Priya Raman priya@example.com SUPERSECRETTOKEN'


def open_incident(monitor, base=None, **result_fields):
    from incidents.services import process_check_result

    base = base or timezone.now() - timedelta(hours=1)
    fields = {
        'is_success': False,
        'error_type': ErrorType.HTTP_ERROR,
        'error_message': 'HTTP 503 Service Unavailable',
        'status_code': 503,
    }
    fields.update(result_fields)
    for offset in (0, 1):
        result = CheckResult.objects.create(
            monitor=monitor, checked_at=base + timedelta(minutes=offset), **fields
        )
        process_check_result(result)
    return Incident.objects.get()


def resolve_incident(monitor, base):
    from incidents.services import process_check_result

    for offset in (2, 3):
        result = CheckResult.objects.create(
            monitor=monitor,
            checked_at=base + timedelta(minutes=offset),
            is_success=True,
            status_code=200,
        )
        process_check_result(result)


class TestPreferenceApi:
    def test_defaults_are_returned_for_a_new_user(self, auth_client):
        response = auth_client.get(reverse(PREFERENCES_URL))

        assert response.status_code == 200
        assert response.json()['email_enabled'] is True
        assert response.json()['alert_email'] is None

    def test_reading_creates_the_row_once(self, auth_client, user):
        auth_client.get(reverse(PREFERENCES_URL))
        auth_client.get(reverse(PREFERENCES_URL))

        assert NotificationPreference.objects.filter(user=user).count() == 1

    def test_email_can_be_disabled(self, auth_client, user):
        response = auth_client.patch(
            reverse(PREFERENCES_URL), {'email_enabled': False}, format='json'
        )

        assert response.status_code == 200
        assert NotificationPreference.objects.get(user=user).email_enabled is False

    def test_a_custom_address_can_be_set(self, auth_client, user):
        response = auth_client.patch(
            reverse(PREFERENCES_URL), {'alert_email': 'ops@example.com'}, format='json'
        )

        assert response.status_code == 200
        assert NotificationPreference.objects.get(user=user).alert_email == 'ops@example.com'

    def test_a_blank_address_falls_back_to_the_account(self, auth_client, user):
        auth_client.patch(
            reverse(PREFERENCES_URL), {'alert_email': 'ops@example.com'}, format='json'
        )

        auth_client.patch(reverse(PREFERENCES_URL), {'alert_email': '   '}, format='json')

        assert NotificationPreference.objects.get(user=user).alert_email is None

    def test_an_invalid_address_is_rejected(self, auth_client):
        response = auth_client.patch(
            reverse(PREFERENCES_URL), {'alert_email': 'not-an-address'}, format='json'
        )

        assert response.status_code == 400
        assert 'alert_email' in response.json()

    def test_authentication_is_required(self, api_client):
        assert api_client.get(reverse(PREFERENCES_URL)).status_code == 401

    def test_each_user_sees_only_their_own(self, auth_client, other_client, user, other_user):
        auth_client.patch(
            reverse(PREFERENCES_URL), {'alert_email': 'mine@example.com'}, format='json'
        )

        body = other_client.get(reverse(PREFERENCES_URL)).json()

        assert body['alert_email'] is None
        assert NotificationPreference.objects.get(user=user).alert_email == 'mine@example.com'

    def test_the_user_field_cannot_be_reassigned(self, auth_client, user, other_user):
        auth_client.patch(reverse(PREFERENCES_URL), {'user': other_user.pk}, format='json')

        assert NotificationPreference.objects.get(user=user).user_id == user.pk

    def test_delivery_internals_are_not_exposed(self, auth_client):
        body = auth_client.get(reverse(PREFERENCES_URL)).json()

        assert set(body) == {'email_enabled', 'alert_email', 'created_at', 'updated_at'}


class TestOutageEmail:
    @pytest.fixture
    def event(self, monitor):
        open_incident(monitor)
        return NotificationEvent.objects.get()

    def test_the_subject_names_the_host(self, event, website):
        subject, _, _ = render(event)

        assert subject == '[Uptora] Problem detected on example.com'

    def test_the_body_identifies_the_site(self, event, website):
        _, text, _ = render(event)

        assert website.name in text
        assert website.url in text

    def test_the_body_reports_the_failure(self, event):
        _, text, _ = render(event)

        assert 'HTTP 503' in text
        assert 'HTTP error' in text

    def test_the_body_reports_the_check_type(self, event):
        _, text, _ = render(event)

        assert 'HTTP' in text

    def test_the_body_states_when_it_started(self, event):
        incident = Incident.objects.get()
        _, text, _ = render(event)

        assert f'{incident.started_at:%Y-%m-%d}' in text

    def test_the_html_body_is_produced(self, event):
        _, _, html = render(event)

        assert html.startswith('<div')
        assert 'Problem detected' in html

    def test_the_body_links_only_to_the_app_base(self, event, settings):
        _, text, _ = render(event)

        assert settings.APP_BASE_URL.rstrip('/') in text


class TestRecoveryEmail:
    @pytest.fixture
    def event(self, monitor):
        base = timezone.now() - timedelta(hours=1)
        open_incident(monitor, base=base)
        resolve_incident(monitor, base)
        return NotificationEvent.objects.get(event_type=EventType.INCIDENT_RESOLVED)

    def test_the_subject_says_recovered(self, event):
        subject, _, _ = render(event)

        assert subject == '[Uptora] example.com has recovered'

    def test_the_body_reports_the_duration(self, event):
        _, text, _ = render(event)

        assert 'Duration:' in text
        assert '3m 0s' in text

    def test_the_body_reports_both_timestamps(self, event):
        incident = Incident.objects.get()
        _, text, _ = render(event)

        assert f'{incident.started_at:%H:%M:%S}' in text
        assert f'{incident.resolved_at:%H:%M:%S}' in text

    def test_the_body_reports_the_failed_check_count(self, event):
        _, text, _ = render(event)

        assert 'Failed checks: 2' in text

    def test_the_html_body_is_produced(self, event):
        _, _, html = render(event)

        assert 'has recovered' in html


class TestDurationFormatting:
    @pytest.mark.parametrize(
        ('seconds', 'expected'),
        [(None, 'unknown'), (5, '5 seconds'), (90, '1m 30s'), (3720, '1h 2m')],
    )
    def test_it_reads_naturally(self, seconds, expected):
        assert humanize_duration(seconds) == expected


class TestSensitiveValues:
    """A contact form's fields may hold a real person's details."""

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
        open_incident(
            monitor,
            error_type=ErrorType.FLOW_SUCCESS_TEXT_MISSING,
            error_message="Submitted, but the success text never appeared: 'Thanks'",
            status_code=200,
        )
        return NotificationEvent.objects.get()

    def test_the_email_body_carries_no_configured_values(self, flow_event):
        subject, text, html = render(flow_event)

        for blob in (subject, text, html):
            assert SECRET_VALUE not in blob
            assert 'SUPERSECRETTOKEN' not in blob
            assert 'priya@example.com' not in blob

    def test_the_event_row_carries_no_configured_values(self, flow_event):
        assert SECRET_VALUE not in repr(flow_event.__dict__)

    def test_the_delivery_row_carries_no_configured_values(self, flow_event):
        delivery = NotificationDelivery.objects.get()

        assert SECRET_VALUE not in repr(delivery.__dict__)

    def test_what_the_provider_receives_carries_no_configured_values(self, flow_event):
        provider = FakeEmailProvider()

        send_delivery(NotificationDelivery.objects.get().id, provider=provider)

        assert SECRET_VALUE not in repr(provider.last)

    def test_the_logs_carry_no_configured_values(self, flow_event, caplog):
        import logging

        provider = FakeEmailProvider()
        with caplog.at_level(logging.DEBUG):
            send_delivery(NotificationDelivery.objects.get().id, provider=provider)

        assert SECRET_VALUE not in caplog.text
        assert 'SUPERSECRETTOKEN' not in caplog.text

    def test_no_lease_token_reaches_the_email(self, monitor):
        open_incident(monitor)
        delivery = NotificationDelivery.objects.get()
        provider = FakeEmailProvider()

        send_delivery(delivery.id, provider=provider)

        stored = NotificationDelivery.objects.get(pk=delivery.id)
        assert str(stored.claim_token) not in repr(provider.last)


class TestScreenshotEvidence:
    def test_an_email_mentions_evidence_without_linking_to_it(self, monitor, settings):
        from django.core.files.base import ContentFile

        from incidents.services import process_check_result

        base = timezone.now() - timedelta(hours=1)
        for offset in (0, 1):
            result = CheckResult.objects.create(
                monitor=monitor,
                checked_at=base + timedelta(minutes=offset),
                is_success=False,
                error_type=ErrorType.HTTP_ERROR,
                error_message='HTTP 500',
                status_code=500,
            )
            result.screenshot.save('shot.png', ContentFile(b'PNGDATA'), save=True)
            process_check_result(result)

        _, text, _ = render(NotificationEvent.objects.get())

        assert 'available in Uptora' in text
        # No raw media path: serving those behind auth is still deferred.
        assert 'check-screenshots/' not in text

    def test_an_email_without_evidence_says_nothing_about_it(self, monitor):
        open_incident(monitor)

        _, text, _ = render(NotificationEvent.objects.get())

        assert 'available in Uptora' not in text
