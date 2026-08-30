import socket

import httpx
import pytest
from django.urls import reverse

from monitors import ssrf
from monitors.checks import tls
from monitors.models import CheckResult, Monitor

pytestmark = pytest.mark.django_db

LIST_URL = 'monitor-list'
DETAIL_URL = 'monitor-detail'
RUN_URL = 'monitor-run'
RESULTS_URL = 'monitor-results'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Nothing in this module is allowed to touch the network."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)


@pytest.fixture
def stub_response(monkeypatch):
    """Make execute_monitor answer with a canned HTTP response."""
    import monitors.views as views
    from monitors.execution import execute_monitor

    def configure(status_code=200):
        transport = httpx.MockTransport(lambda request: httpx.Response(status_code))
        monkeypatch.setattr(
            views,
            'execute_monitor',
            lambda monitor: execute_monitor(monitor, transport=transport),
        )

    return configure


def payload(website, **overrides):
    data = {'website': website.pk, 'monitor_type': 'HTTP'}
    data.update(overrides)
    return data


class TestMonitorCrud:
    def test_user_can_create_an_http_monitor(self, auth_client, website):
        response = auth_client.post(reverse(LIST_URL), payload(website), format='json')

        assert response.status_code == 201
        monitor = Monitor.objects.get(pk=response.json()['id'])
        assert monitor.website == website
        assert monitor.monitor_type == 'HTTP'

    def test_defaults_are_applied(self, auth_client, website):
        response = auth_client.post(reverse(LIST_URL), payload(website), format='json')

        body = response.json()
        assert body['interval_seconds'] == 300
        assert body['timeout_seconds'] == 10
        assert body['is_enabled'] is True

    def test_unauthenticated_user_cannot_create_a_monitor(self, api_client, website):
        response = api_client.post(reverse(LIST_URL), payload(website), format='json')

        assert response.status_code == 401
        assert not Monitor.objects.exists()

    def test_cannot_attach_a_monitor_to_another_users_website(self, auth_client, other_website):
        response = auth_client.post(reverse(LIST_URL), payload(other_website), format='json')

        assert response.status_code == 400
        assert 'website' in response.json()
        assert not Monitor.objects.exists()

    @pytest.mark.parametrize('monitor_type', ['FLOW', 'PING', 'http', 'CHROME'])
    def test_unsupported_monitor_types_are_rejected(self, auth_client, website, monitor_type):
        response = auth_client.post(
            reverse(LIST_URL), payload(website, monitor_type=monitor_type), format='json'
        )

        assert response.status_code == 400
        assert 'monitor_type' in response.json()

    def test_list_shows_only_own_monitors(self, auth_client, monitor, other_monitor):
        response = auth_client.get(reverse(LIST_URL))

        assert response.status_code == 200
        assert [item['id'] for item in response.json()] == [monitor.pk]

    def test_user_can_retrieve_own_monitor(self, auth_client, monitor):
        response = auth_client.get(reverse(DETAIL_URL, args=[monitor.pk]))

        assert response.status_code == 200
        assert response.json()['id'] == monitor.pk

    def test_another_users_monitor_is_not_found(self, auth_client, other_monitor):
        response = auth_client.get(reverse(DETAIL_URL, args=[other_monitor.pk]))

        assert response.status_code == 404

    def test_user_can_update_own_monitor(self, auth_client, monitor):
        response = auth_client.patch(
            reverse(DETAIL_URL, args=[monitor.pk]), {'interval_seconds': 900}, format='json'
        )

        assert response.status_code == 200
        monitor.refresh_from_db()
        assert monitor.interval_seconds == 900

    def test_user_cannot_update_another_users_monitor(self, auth_client, other_monitor):
        response = auth_client.patch(
            reverse(DETAIL_URL, args=[other_monitor.pk]),
            {'is_enabled': False},
            format='json',
        )

        assert response.status_code == 404
        other_monitor.refresh_from_db()
        assert other_monitor.is_enabled

    def test_user_can_delete_own_monitor(self, auth_client, monitor):
        response = auth_client.delete(reverse(DETAIL_URL, args=[monitor.pk]))

        assert response.status_code == 204
        assert not Monitor.objects.filter(pk=monitor.pk).exists()

    def test_user_cannot_delete_another_users_monitor(self, auth_client, other_monitor):
        response = auth_client.delete(reverse(DETAIL_URL, args=[other_monitor.pk]))

        assert response.status_code == 404
        assert Monitor.objects.filter(pk=other_monitor.pk).exists()


class TestConfigurationValidation:
    @pytest.mark.parametrize('interval', [0, 1, 59, -10, 86401])
    def test_interval_outside_the_supported_range_is_rejected(self, auth_client, website, interval):
        response = auth_client.post(
            reverse(LIST_URL), payload(website, interval_seconds=interval), format='json'
        )

        assert response.status_code == 400
        assert 'interval_seconds' in response.json()

    @pytest.mark.parametrize('interval', [60, 300, 86400])
    def test_intervals_inside_the_supported_range_are_accepted(
        self, auth_client, website, interval
    ):
        response = auth_client.post(
            reverse(LIST_URL), payload(website, interval_seconds=interval), format='json'
        )

        assert response.status_code == 201

    @pytest.mark.parametrize('timeout', [0, -1, 31, 120])
    def test_timeout_outside_the_supported_range_is_rejected(self, auth_client, website, timeout):
        response = auth_client.post(
            reverse(LIST_URL), payload(website, timeout_seconds=timeout), format='json'
        )

        assert response.status_code == 400
        assert 'timeout_seconds' in response.json()

    @pytest.mark.parametrize('timeout', [1, 10, 30])
    def test_timeouts_inside_the_supported_range_are_accepted(self, auth_client, website, timeout):
        response = auth_client.post(
            reverse(LIST_URL), payload(website, timeout_seconds=timeout), format='json'
        )

        assert response.status_code == 201


class TestManualRun:
    def test_owner_can_run_a_monitor(self, auth_client, monitor, stub_response):
        stub_response(200)

        response = auth_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 201
        body = response.json()
        assert body['is_success'] is True
        assert body['status_code'] == 200
        assert body['monitor'] == monitor.pk
        assert CheckResult.objects.count() == 1

    def test_a_failing_target_still_returns_a_result(self, auth_client, monitor, stub_response):
        stub_response(503)

        response = auth_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 201
        assert response.json()['is_success'] is False
        assert response.json()['error_type'] == 'HTTP_ERROR'

    def test_disabled_monitor_cannot_be_run(self, auth_client, monitor):
        monitor.is_enabled = False
        monitor.save(update_fields=['is_enabled'])

        response = auth_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 409
        assert not CheckResult.objects.exists()

    def test_only_the_owner_can_run_a_monitor(self, other_client, monitor):
        response = other_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 404
        assert not CheckResult.objects.exists()

    def test_unauthenticated_run_is_rejected(self, api_client, monitor):
        response = api_client.post(reverse(RUN_URL, args=[monitor.pk]))

        assert response.status_code == 401
        assert not CheckResult.objects.exists()


class TestResults:
    def test_owner_sees_their_results_newest_first(self, auth_client, monitor):
        for status_code in (200, 500, 200):
            CheckResult.objects.create(
                monitor=monitor, is_success=status_code == 200, status_code=status_code
            )

        response = auth_client.get(reverse(RESULTS_URL, args=[monitor.pk]))

        assert response.status_code == 200
        body = response.json()
        assert body['count'] == 3
        ids = [item['id'] for item in body['results']]
        assert ids == sorted(ids, reverse=True)

    def test_results_are_paginated(self, auth_client, monitor):
        CheckResult.objects.bulk_create(
            CheckResult(monitor=monitor, is_success=True, status_code=200) for _ in range(60)
        )

        response = auth_client.get(reverse(RESULTS_URL, args=[monitor.pk]))

        body = response.json()
        assert body['count'] == 60
        assert len(body['results']) == 50
        assert body['next'] is not None

    def test_page_size_is_capped(self, auth_client, monitor):
        CheckResult.objects.bulk_create(
            CheckResult(monitor=monitor, is_success=True, status_code=200) for _ in range(60)
        )

        response = auth_client.get(reverse(RESULTS_URL, args=[monitor.pk]), {'page_size': 5000})

        assert len(response.json()['results']) <= 200

    def test_another_users_results_are_not_reachable(self, auth_client, other_monitor):
        CheckResult.objects.create(monitor=other_monitor, is_success=True, status_code=200)

        response = auth_client.get(reverse(RESULTS_URL, args=[other_monitor.pk]))

        assert response.status_code == 404

    def test_unauthenticated_results_access_is_rejected(self, api_client, monitor):
        response = api_client.get(reverse(RESULTS_URL, args=[monitor.pk]))

        assert response.status_code == 401


def test_website_list_response_is_still_an_unpaginated_array(auth_client, website):
    """Pagination is scoped to the results action and must not leak elsewhere."""
    response = auth_client.get(reverse('website-list'))

    assert isinstance(response.json(), list)
