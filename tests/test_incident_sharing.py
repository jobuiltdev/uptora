import json
from datetime import timedelta
from urllib.parse import urlparse

import pytest
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from incidents.models import Incident, IncidentShare, IncidentStatus
from incidents.sharing import TIMELINE_LIMIT, build_share_snapshot
from monitors.models import CheckResult, ErrorType

pytestmark = pytest.mark.django_db


def make_incident(monitor, *, status=IncidentStatus.OPEN, started_at=None):
    started_at = started_at or timezone.now() - timedelta(minutes=10)
    resolved_at = started_at + timedelta(minutes=5) if status == IncidentStatus.RESOLVED else None
    return Incident.objects.create(
        monitor=monitor,
        status=status,
        started_at=started_at,
        resolved_at=resolved_at,
        failure_type=ErrorType.HTTP_ERROR,
        initial_error_message='private raw failure detail',
        latest_error_message='private latest detail',
        initial_status_code=503,
        latest_status_code=503,
        failure_count=2,
        recovery_count=2 if resolved_at else 0,
    )


def add_failure(incident, *, screenshot=False, checked_at=None, monitor=None):
    result = CheckResult.objects.create(
        monitor=monitor or incident.monitor,
        checked_at=checked_at or incident.started_at,
        is_success=False,
        status_code=503,
        error_type=ErrorType.HTTP_ERROR,
        error_message='raw exception /srv/private.py secret-cookie=abc',
    )
    if screenshot:
        result.screenshot.save('ignored.png', ContentFile(b'PNGDATA'), save=True)
    return result


def token_from(response):
    return urlparse(response.json()['share_url']).path.rsplit('/', 1)[-1]


def create_share(client, incident, **payload):
    return client.post(reverse('incident-share', args=[incident.pk]), payload, format='json')


def test_owner_can_create_and_inspect_one_active_share(auth_client, monitor):
    incident = make_incident(monitor)
    add_failure(incident)
    response = create_share(auth_client, incident, include_evidence=True)
    assert response.status_code == 201
    assert response.json()['share_url'].startswith('http://localhost:3000/share/incidents/')
    assert len(token_from(response)) >= 43
    assert auth_client.get(reverse('incident-share', args=[incident.pk])).status_code == 200
    assert create_share(auth_client, incident).status_code == 409
    assert IncidentShare.objects.filter(incident=incident, revoked_at__isnull=True).count() == 1


def test_expired_unrevoked_share_is_replaced_by_create(
    auth_client,
    api_client,
    monitor,
):
    incident = make_incident(monitor)
    old_token = token_from(create_share(auth_client, incident))
    old_share = IncidentShare.objects.get(token=old_token)
    old_share.expires_at = timezone.now() - timedelta(seconds=1)
    old_share.save(update_fields=['expires_at'])

    response = create_share(auth_client, incident)

    assert response.status_code == 201
    new_token = token_from(response)
    assert new_token != old_token
    old_share.refresh_from_db()
    assert old_share.revoked_at is not None
    assert IncidentShare.objects.filter(incident=incident, revoked_at__isnull=True).count() == 1
    assert api_client.get(reverse('public-incident-share', args=[old_token])).status_code == 404
    assert api_client.get(reverse('public-incident-share', args=[new_token])).status_code == 200


def test_foreign_owner_cannot_create_view_revoke_or_regenerate(
    auth_client,
    other_client,
    monitor,
):
    incident = make_incident(monitor)
    create_share(auth_client, incident)
    url = reverse('incident-share', args=[incident.pk])
    regenerate = reverse('incident-regenerate-share', args=[incident.pk])
    assert other_client.post(url, {}, format='json').status_code == 404
    assert other_client.get(url).status_code == 404
    assert other_client.delete(url).status_code == 404
    assert other_client.post(regenerate, {}, format='json').status_code == 404
    assert IncidentShare.objects.get(incident=incident).revoked_at is None


def test_tokens_are_high_entropy_and_unique(
    auth_client,
    other_client,
    monitor,
    other_monitor,
):
    first = make_incident(monitor, started_at=timezone.now() - timedelta(hours=2))
    second = make_incident(other_monitor, started_at=timezone.now() - timedelta(hours=1))
    tokens = {
        token_from(create_share(auth_client, first)),
        token_from(create_share(other_client, second)),
    }
    assert len(tokens) == 2
    assert all(len(token) >= 43 for token in tokens)


def test_public_valid_token_works_without_auth(auth_client, api_client, monitor):
    incident = make_incident(monitor)
    add_failure(incident)
    token = token_from(create_share(auth_client, incident))
    response = api_client.get(reverse('public-incident-share', args=[token]))
    assert response.status_code == 200
    assert response.json()['website_name'] == monitor.website.name
    assert response.json()['status'] == IncidentStatus.OPEN
    assert response.json()['duration_seconds'] >= 0


@pytest.mark.parametrize('state', ['invalid', 'expired', 'revoked'])
def test_unavailable_tokens_have_the_same_public_response(
    state,
    auth_client,
    api_client,
    monitor,
):
    incident = make_incident(monitor)
    token = 'not-a-real-share-token'
    if state != 'invalid':
        token = token_from(create_share(auth_client, incident))
        share = IncidentShare.objects.get(token=token)
        if state == 'expired':
            share.expires_at = timezone.now() - timedelta(seconds=1)
        else:
            share.revoked_at = timezone.now()
        share.save()
    response = api_client.get(reverse('public-incident-share', args=[token]))
    assert response.status_code == 404
    assert response.json() == {'detail': 'Not found.'}


def test_public_payload_omits_raw_and_sensitive_fields(auth_client, api_client, monitor):
    incident = make_incident(monitor)
    add_failure(incident)
    token = token_from(create_share(auth_client, incident))
    payload = api_client.get(reverse('public-incident-share', args=[token])).json()
    rendered = json.dumps(payload)
    assert 'owner@example.com' not in rendered
    assert 'private raw failure detail' not in rendered
    assert 'secret-cookie' not in rendered
    assert '/srv/private.py' not in rendered
    assert 'owner' not in payload
    assert 'incident_id' not in payload
    assert 'monitor_id' not in payload


def test_timeline_is_bounded_and_keeps_the_final_result(auth_client, api_client, monitor):
    incident = make_incident(monitor, status=IncidentStatus.RESOLVED)
    for index in range(TIMELINE_LIMIT + 20):
        checked_at = incident.started_at + timedelta(seconds=index)
        CheckResult.objects.create(
            monitor=monitor,
            checked_at=checked_at,
            is_success=index == TIMELINE_LIMIT + 19,
            status_code=503 if index < TIMELINE_LIMIT + 19 else 200,
            error_type=ErrorType.HTTP_ERROR if index < TIMELINE_LIMIT + 19 else None,
        )
    token = token_from(create_share(auth_client, incident))
    payload = api_client.get(reverse('public-incident-share', args=[token])).json()
    assert len(payload['timeline']) == TIMELINE_LIMIT
    assert payload['timeline_total_count'] == TIMELINE_LIMIT + 20
    assert payload['timeline'][0]['label'] == 'Initial failure'
    assert payload['timeline'][-1]['outcome'] == 'RECOVERY'


def test_public_evidence_requires_share_permission(auth_client, api_client, monitor):
    incident = make_incident(monitor)
    add_failure(incident, screenshot=True)
    token = token_from(create_share(auth_client, incident, include_evidence=True))
    response = api_client.get(reverse('public-incident-share-evidence', args=[token]))
    assert response.status_code == 200
    assert response['Content-Type'] == 'image/png'
    assert response['Content-Disposition'].startswith('inline;')
    assert b''.join(response.streaming_content) == b'PNGDATA'
    assert 'check-screenshots' not in response['Content-Disposition']


def test_include_evidence_false_blocks_public_evidence(auth_client, api_client, monitor):
    incident = make_incident(monitor)
    add_failure(incident, screenshot=True)
    token = token_from(create_share(auth_client, incident, include_evidence=False))
    response = api_client.get(reverse('public-incident-share-evidence', args=[token]))
    assert response.status_code == 404
    report = api_client.get(reverse('public-incident-share', args=[token])).json()
    assert report['evidence_available'] is False


def test_evidence_from_another_incident_is_rejected(
    auth_client,
    api_client,
    monitor,
    other_monitor,
):
    incident = make_incident(monitor)
    foreign_incident = make_incident(other_monitor)
    foreign_result = add_failure(foreign_incident, screenshot=True)
    token = token_from(create_share(auth_client, incident, include_evidence=True))
    share = IncidentShare.objects.get(token=token)
    share.evidence_result = foreign_result
    share.save(update_fields=['evidence_result'])
    response = api_client.get(reverse('public-incident-share-evidence', args=[token]))
    assert response.status_code == 404


def test_existing_authenticated_evidence_endpoint_is_unchanged(auth_client, monitor):
    incident = make_incident(monitor)
    result = add_failure(incident, screenshot=True)
    response = auth_client.get(reverse('check-result-evidence', args=[result.pk]))
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'PNGDATA'


def test_snapshot_survives_check_result_cleanup(auth_client, api_client, monitor):
    incident = make_incident(monitor)
    result = add_failure(incident)
    token = token_from(create_share(auth_client, incident))
    expected_timeline = IncidentShare.objects.get(token=token).snapshot['timeline']
    result.delete()
    incident.status = IncidentStatus.RESOLVED
    incident.resolved_at = timezone.now()
    incident.recovery_count = 2
    incident.save(update_fields=['status', 'resolved_at', 'recovery_count', 'updated_at'])
    CheckResult.objects.create(
        monitor=monitor,
        checked_at=incident.resolved_at,
        is_success=True,
        status_code=200,
    )
    response = api_client.get(reverse('public-incident-share', args=[token]))
    assert response.status_code == 200
    assert response.json()['status'] == IncidentStatus.RESOLVED
    assert response.json()['timeline'][0] == expected_timeline[0]
    assert response.json()['timeline'][-1]['label'] == 'Incident resolved'


def test_model_token_generator_honors_database_uniqueness(monitor, other_monitor):
    first = make_incident(monitor, started_at=timezone.now() - timedelta(hours=2))
    second = make_incident(other_monitor, started_at=timezone.now() - timedelta(hours=1))
    IncidentShare.objects.create(
        incident=first,
        token='same-token',
        snapshot=build_share_snapshot(first),
    )
    with pytest.raises(IntegrityError):
        IncidentShare.objects.create(
            incident=second,
            token='same-token',
            snapshot=build_share_snapshot(second),
        )
