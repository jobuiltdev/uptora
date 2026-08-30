import socket
from datetime import timedelta

import httpx
import pytest
from django.urls import reverse
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from monitors import ssrf
from monitors.checks import tls
from monitors.execution import execute_monitor
from monitors.models import CheckResult, ErrorType

pytestmark = pytest.mark.django_db

LIST_URL = 'incident-list'
DETAIL_URL = 'incident-detail'


def make_incident(monitor, status=IncidentStatus.OPEN, **overrides):
    started_at = overrides.pop('started_at', timezone.now() - timedelta(minutes=30))
    fields = {
        'monitor': monitor,
        'status': status,
        'started_at': started_at,
        'failure_type': ErrorType.TIMEOUT,
        'initial_error_message': 'timed out',
        'latest_error_message': 'timed out',
        'failure_count': 2,
    }
    if status == IncidentStatus.RESOLVED:
        fields['resolved_at'] = started_at + timedelta(minutes=10)
    fields.update(overrides)
    return Incident.objects.create(**fields)


class TestIncidentAccess:
    def test_user_sees_only_their_own_incidents(self, auth_client, monitor, other_monitor):
        mine = make_incident(monitor)
        make_incident(other_monitor)

        response = auth_client.get(reverse(LIST_URL))

        assert response.status_code == 200
        assert [item['id'] for item in response.json()['results']] == [mine.pk]

    def test_owner_can_retrieve_their_incident(self, auth_client, monitor):
        incident = make_incident(monitor)

        response = auth_client.get(reverse(DETAIL_URL, args=[incident.pk]))

        assert response.status_code == 200
        assert response.json()['id'] == incident.pk

    def test_another_users_incident_is_not_found(self, auth_client, other_monitor):
        incident = make_incident(other_monitor)

        response = auth_client.get(reverse(DETAIL_URL, args=[incident.pk]))

        assert response.status_code == 404

    def test_unauthenticated_access_is_rejected(self, api_client, monitor):
        make_incident(monitor)

        assert api_client.get(reverse(LIST_URL)).status_code == 401

    def test_incidents_are_newest_first(self, auth_client, monitor):
        now = timezone.now()
        older = make_incident(
            monitor, status=IncidentStatus.RESOLVED, started_at=now - timedelta(days=2)
        )
        newer = make_incident(monitor, started_at=now - timedelta(hours=1))

        response = auth_client.get(reverse(LIST_URL))

        assert [item['id'] for item in response.json()['results']] == [newer.pk, older.pk]


class TestReadOnly:
    def test_create_is_not_allowed(self, auth_client, monitor):
        response = auth_client.post(reverse(LIST_URL), {'monitor': monitor.pk}, format='json')

        assert response.status_code == 405

    def test_update_is_not_allowed(self, auth_client, monitor):
        incident = make_incident(monitor)

        response = auth_client.patch(
            reverse(DETAIL_URL, args=[incident.pk]),
            {'status': IncidentStatus.RESOLVED},
            format='json',
        )

        assert response.status_code == 405
        incident.refresh_from_db()
        assert incident.status == IncidentStatus.OPEN

    def test_delete_is_not_allowed(self, auth_client, monitor):
        incident = make_incident(monitor)

        response = auth_client.delete(reverse(DETAIL_URL, args=[incident.pk]))

        assert response.status_code == 405
        assert Incident.objects.filter(pk=incident.pk).exists()


class TestFiltering:
    @pytest.fixture
    def incidents(self, monitor, other_monitor):
        now = timezone.now()
        return {
            'open': make_incident(monitor, started_at=now - timedelta(hours=1)),
            'resolved': make_incident(
                monitor, status=IncidentStatus.RESOLVED, started_at=now - timedelta(days=1)
            ),
            'foreign': make_incident(other_monitor),
        }

    def test_status_open_filter(self, auth_client, incidents):
        response = auth_client.get(reverse(LIST_URL), {'status': 'OPEN'})

        assert [item['id'] for item in response.json()['results']] == [incidents['open'].pk]

    def test_status_resolved_filter(self, auth_client, incidents):
        response = auth_client.get(reverse(LIST_URL), {'status': 'RESOLVED'})

        assert [item['id'] for item in response.json()['results']] == [incidents['resolved'].pk]

    def test_status_filter_is_case_insensitive(self, auth_client, incidents):
        response = auth_client.get(reverse(LIST_URL), {'status': 'open'})

        assert [item['id'] for item in response.json()['results']] == [incidents['open'].pk]

    def test_unknown_status_is_rejected(self, auth_client, incidents):
        response = auth_client.get(reverse(LIST_URL), {'status': 'BROKEN'})

        assert response.status_code == 400
        assert 'status' in response.json()

    def test_monitor_filter(self, auth_client, incidents, monitor):
        response = auth_client.get(reverse(LIST_URL), {'monitor': monitor.pk})

        ids = {item['id'] for item in response.json()['results']}
        assert ids == {incidents['open'].pk, incidents['resolved'].pk}

    def test_monitor_filter_cannot_reach_another_users_incidents(
        self, auth_client, incidents, other_monitor
    ):
        response = auth_client.get(reverse(LIST_URL), {'monitor': other_monitor.pk})

        assert response.json()['results'] == []

    def test_non_numeric_monitor_filter_is_rejected(self, auth_client, incidents):
        response = auth_client.get(reverse(LIST_URL), {'monitor': 'abc'})

        assert response.status_code == 400
        assert 'monitor' in response.json()

    def test_filters_combine(self, auth_client, incidents, monitor):
        response = auth_client.get(reverse(LIST_URL), {'monitor': monitor.pk, 'status': 'RESOLVED'})

        assert [item['id'] for item in response.json()['results']] == [incidents['resolved'].pk]


class TestPayload:
    def test_identifies_what_failed_without_nesting_whole_objects(
        self, auth_client, monitor, website
    ):
        incident = make_incident(monitor)

        body = auth_client.get(reverse(DETAIL_URL, args=[incident.pk])).json()

        assert body['monitor'] == monitor.pk
        assert body['website'] == website.pk
        assert body['website_name'] == website.name
        assert body['website_url'] == website.url
        # Flat identifiers, not embedded resources.
        assert not isinstance(body['monitor'], dict)

    def test_open_incident_has_no_duration(self, auth_client, monitor):
        incident = make_incident(monitor)

        body = auth_client.get(reverse(DETAIL_URL, args=[incident.pk])).json()

        assert body['resolved_at'] is None
        assert body['duration_seconds'] is None

    def test_resolved_incident_reports_its_duration(self, auth_client, monitor):
        incident = make_incident(monitor, status=IncidentStatus.RESOLVED)

        body = auth_client.get(reverse(DETAIL_URL, args=[incident.pk])).json()

        assert body['duration_seconds'] == 600

    def test_failure_detail_is_exposed_for_a_dashboard(self, auth_client, monitor):
        incident = make_incident(
            monitor,
            failure_type=ErrorType.HTTP_ERROR,
            initial_status_code=500,
            latest_status_code=503,
            latest_error_message='HTTP 503',
            failure_count=4,
            recovery_count=1,
        )

        body = auth_client.get(reverse(DETAIL_URL, args=[incident.pk])).json()

        assert body['failure_type'] == 'HTTP_ERROR'
        assert body['initial_status_code'] == 500
        assert body['latest_status_code'] == 503
        assert body['latest_error_message'] == 'HTTP 503'
        assert body['failure_count'] == 4
        assert body['recovery_count'] == 1


class TestMonitorSerializerIntegration:
    def test_monitor_without_an_incident_reports_false(self, auth_client, monitor):
        body = auth_client.get(reverse('monitor-detail', args=[monitor.pk])).json()

        assert body['has_open_incident'] is False

    def test_monitor_with_an_open_incident_reports_true(self, auth_client, monitor):
        make_incident(monitor)

        body = auth_client.get(reverse('monitor-detail', args=[monitor.pk])).json()

        assert body['has_open_incident'] is True

    def test_resolved_incident_does_not_mark_the_monitor_as_down(self, auth_client, monitor):
        make_incident(monitor, status=IncidentStatus.RESOLVED)

        body = auth_client.get(reverse('monitor-detail', args=[monitor.pk])).json()

        assert body['has_open_incident'] is False

    def test_flag_is_present_in_the_list_view(self, auth_client, monitor):
        make_incident(monitor)

        body = auth_client.get(reverse('monitor-list')).json()

        assert [item['has_open_incident'] for item in body] == [True]

    def test_flag_is_present_on_a_freshly_created_monitor(self, auth_client, website):
        response = auth_client.post(
            reverse('monitor-list'),
            {'website': website.pk, 'monitor_type': 'HTTP'},
            format='json',
        )

        assert response.status_code == 201
        assert response.json()['has_open_incident'] is False

    def test_monitor_history_is_not_embedded(self, auth_client, monitor):
        make_incident(monitor)

        body = auth_client.get(reverse('monitor-detail', args=[monitor.pk])).json()

        assert 'incidents' not in body


class TestExecutionIntegration:
    """execute_monitor must record the check and move incident state together."""

    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

        monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
        monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)

    @staticmethod
    def transport(status_code):
        return httpx.MockTransport(lambda request: httpx.Response(status_code))

    def run(self, monitor, status_code):
        return execute_monitor(monitor, transport=self.transport(status_code))

    def test_one_execution_records_one_result_and_processes_it(self, monitor):
        result = self.run(monitor, 500)

        result.refresh_from_db()
        assert CheckResult.objects.count() == 1
        assert result.incident_processed_at is not None
        assert not Incident.objects.exists()

    def test_repeated_executions_walk_the_expected_transitions(self, monitor):
        self.run(monitor, 500)
        assert not Incident.objects.exists()

        self.run(monitor, 500)
        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.failure_count == 2

        self.run(monitor, 500)
        incident.refresh_from_db()
        assert incident.failure_count == 3

        self.run(monitor, 200)
        incident.refresh_from_db()
        assert incident.status == IncidentStatus.OPEN
        assert incident.recovery_count == 1

        self.run(monitor, 200)
        incident.refresh_from_db()
        assert incident.status == IncidentStatus.RESOLVED
        assert CheckResult.objects.count() == 5
        assert Incident.objects.count() == 1

    def test_incident_processing_never_creates_a_check_result(self, monitor):
        self.run(monitor, 500)
        self.run(monitor, 500)

        assert CheckResult.objects.count() == 2

    def test_an_internal_processing_error_is_not_recorded_as_a_site_failure(
        self, monitor, monkeypatch
    ):
        """A bug in our code must not be written down as the customer being down."""
        import monitors.execution as execution

        def explode(check_result):
            raise RuntimeError('bug in the incident engine')

        monkeypatch.setattr(execution, 'process_check_result', explode)

        with pytest.raises(RuntimeError):
            self.run(monitor, 200)

        # The observation survives, unprocessed and truthfully recorded as a
        # success, ready for a retry to pick up.
        result = CheckResult.objects.get()
        assert result.is_success
        assert result.status_code == 200
        assert result.error_type is None
        assert result.incident_processed_at is None
        assert not Incident.objects.exists()

    def test_a_retry_after_an_internal_error_processes_cleanly(self, monitor, monkeypatch):
        import monitors.execution as execution

        self.run(monitor, 500)

        def explode(check_result):
            raise RuntimeError('transient bug')

        monkeypatch.setattr(execution, 'process_check_result', explode)
        with pytest.raises(RuntimeError):
            self.run(monitor, 500)

        monkeypatch.undo()
        unprocessed = CheckResult.objects.get(incident_processed_at__isnull=True)
        from incidents.services import process_check_result

        process_check_result(unprocessed)

        incident = Incident.objects.get()
        assert incident.status == IncidentStatus.OPEN
        assert incident.failure_count == 2
