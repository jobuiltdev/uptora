import logging
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.test import override_settings
from django.urls import reverse

from config.logging import RedactCapabilitiesFilter
from incidents.admin import IncidentShareAdmin
from incidents.models import IncidentShare
from monitors.admin import FlowConfigAdmin, FlowFieldInline
from monitors.models import FlowConfig
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db


@override_settings(REGISTRATION_ENABLED=False)
def test_private_alpha_registration_gate_is_server_enforced(api_client):
    response = api_client.post(
        reverse('register'),
        {'email': 'blocked@example.com', 'password': PASSWORD},
        format='json',
    )
    assert response.status_code == 403
    assert response.json() == {
        'detail': 'Registration is currently limited to private-alpha testers.'
    }


@override_settings(REGISTRATION_ENABLED=False)
def test_existing_user_can_login_while_registration_is_closed(api_client, user):
    response = api_client.post(
        reverse('login'),
        {'email': user.email, 'password': PASSWORD},
        format='json',
    )
    assert response.status_code == 200
    assert 'access' in response.json()


@override_settings(REGISTRATION_ENABLED=False)
def test_registration_status_is_public_and_reports_gate(api_client):
    response = api_client.get(reverse('registration-status'))
    assert response.status_code == 200
    assert response.json() == {'enabled': False}


@override_settings(READINESS_TOKEN='operator-secret')
def test_readiness_is_hidden_without_operator_key(api_client):
    assert api_client.get(reverse('readiness')).status_code == 404
    assert (
        api_client.get(reverse('readiness'), HTTP_X_UPTORA_READINESS_KEY='wrong').status_code == 404
    )


@override_settings(READINESS_TOKEN='operator-secret')
def test_readiness_checks_database_and_redis(api_client, monkeypatch):
    redis_client = Mock()
    redis_client.ping.return_value = True
    monkeypatch.setattr('config.views.Redis.from_url', Mock(return_value=redis_client))
    response = api_client.get(
        reverse('readiness'),
        HTTP_X_UPTORA_READINESS_KEY='operator-secret',
    )
    assert response.status_code == 200
    assert response.json() == {'status': 'ready'}
    redis_client.ping.assert_called_once_with()


@override_settings(READINESS_TOKEN='operator-secret')
def test_readiness_failure_is_generic(api_client, monkeypatch):
    monkeypatch.setattr(
        'config.views.Redis.from_url',
        Mock(side_effect=OSError('redis host and credential must stay private')),
    )
    response = api_client.get(
        reverse('readiness'),
        HTTP_X_UPTORA_READINESS_KEY='operator-secret',
    )
    assert response.status_code == 503
    assert response.json() == {'status': 'not_ready'}
    assert 'redis' not in str(response.json()).lower()


def test_admin_registers_shares_without_tokens_and_hides_flow_values():
    share_admin = admin.site._registry[IncidentShare]
    flow_admin = admin.site._registry[FlowConfig]
    assert isinstance(share_admin, IncidentShareAdmin)
    assert share_admin.exclude == ('token',)
    assert FlowFieldInline in flow_admin.inlines
    assert 'value' not in FlowFieldInline.fields
    assert isinstance(flow_admin, FlowConfigAdmin)


def test_operational_log_filter_redacts_capability_and_bearer_tokens():
    record = logging.LogRecord(
        'uptora',
        logging.INFO,
        __file__,
        1,
        'GET /share/incidents/top-secret-token Authorization %s',
        ('Bearer header.payload.signature',),
        None,
    )
    RedactCapabilitiesFilter().filter(record)
    rendered = record.getMessage()
    assert 'top-secret-token' not in rendered
    assert 'header.payload.signature' not in rendered
    assert rendered.count('[REDACTED]') == 2
