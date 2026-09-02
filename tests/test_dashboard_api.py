from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from incidents.models import Incident, IncidentStatus
from monitors.models import CheckResult, ErrorType, FlowConfig, FlowField, Monitor, MonitorType

pytestmark = pytest.mark.django_db


def test_dashboard_requires_authentication(api_client):
    assert api_client.get(reverse('dashboard')).status_code == 401


def test_dashboard_is_owner_scoped_and_derives_health(auth_client, website, other_website):
    healthy = Monitor.objects.create(website=website)
    degraded = Monitor.objects.create(website=website, monitor_type=MonitorType.BROWSER)
    unknown = Monitor.objects.create(website=website, monitor_type=MonitorType.FLOW)
    disabled = Monitor.objects.create(website=website, is_enabled=False)
    Monitor.objects.create(website=other_website)
    CheckResult.objects.create(monitor=healthy, is_success=True, status_code=200)
    CheckResult.objects.create(
        monitor=degraded,
        is_success=False,
        error_type=ErrorType.HTTP_ERROR,
        error_message='Service unavailable',
    )

    response = auth_client.get(reverse('dashboard'))

    assert response.status_code == 200
    body = response.json()
    assert body['summary']['websites'] == 1
    assert body['summary']['enabled_monitors'] == 3
    assert body['summary']['healthy_monitors'] == 1
    assert body['websites'][0]['health'] == 'DEGRADED'
    assert body['websites'][0]['monitor_count'] == 4
    assert 'value' not in str(body)
    assert unknown.pk
    assert disabled.pk


def test_open_incident_takes_problem_precedence(auth_client, monitor):
    CheckResult.objects.create(monitor=monitor, is_success=False, error_type=ErrorType.TIMEOUT)
    Incident.objects.create(
        monitor=monitor,
        status=IncidentStatus.OPEN,
        started_at=timezone.now(),
        failure_type=ErrorType.TIMEOUT,
        failure_count=2,
    )

    body = auth_client.get(reverse('dashboard')).json()

    assert body['summary']['active_incidents'] == 1
    assert body['websites'][0]['health'] == 'PROBLEM'
    assert body['active_incidents'][0]['monitor_type'] == MonitorType.HTTP


def test_dashboard_counts_only_recent_checks(auth_client, monitor):
    recent = CheckResult.objects.create(monitor=monitor, is_success=True)
    old = CheckResult.objects.create(monitor=monitor, is_success=True)
    CheckResult.objects.filter(pk=old.pk).update(checked_at=timezone.now() - timedelta(days=2))

    assert auth_client.get(reverse('dashboard')).json()['summary']['checks_24h'] == 1
    assert recent.pk


def test_dashboard_never_leaks_flow_values(auth_client, website):
    monitor = Monitor.objects.create(website=website, monitor_type=MonitorType.FLOW)
    config = FlowConfig.objects.create(
        monitor=monitor,
        flow_kind='CONTACT_FORM',
        submit_selector='button',
        success_text='Thanks',
    )
    FlowField.objects.create(
        flow_config=config,
        selector='#email',
        field_type='EMAIL',
        value='secret-dashboard-canary',
    )

    assert 'secret-dashboard-canary' not in str(auth_client.get(reverse('dashboard')).json())
