import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from monitors.models import CheckResult

pytestmark = pytest.mark.django_db


def test_monitor_list_can_filter_by_owned_website(auth_client, website, monitor, other_website):
    own_other = monitor.__class__.objects.create(website=other_website)
    response = auth_client.get(reverse('monitor-list'), {'website': website.pk})
    assert [item['id'] for item in response.json()] == [monitor.pk]
    assert own_other.pk


def test_monitor_filter_rejects_malformed_id(auth_client):
    response = auth_client.get(reverse('monitor-list'), {'website': 'not-an-id'})
    assert response.status_code == 400
    assert 'website' in response.json()


def test_foreign_website_filter_leaks_nothing(auth_client, other_website, other_monitor):
    response = auth_client.get(reverse('monitor-list'), {'website': other_website.pk})
    assert response.status_code == 200
    assert response.json() == []
    assert other_monitor.pk


def test_unfiltered_monitor_list_is_unchanged(auth_client, monitor):
    assert [item['id'] for item in auth_client.get(reverse('monitor-list')).json()] == [monitor.pk]


def test_evidence_requires_authentication(api_client, monitor):
    result = CheckResult.objects.create(monitor=monitor, is_success=False)
    assert api_client.get(reverse('check-result-evidence', args=[result.pk])).status_code == 401


def test_owner_can_stream_evidence(auth_client, monitor):
    result = CheckResult.objects.create(monitor=monitor, is_success=False)
    result.screenshot.save('ignored.png', ContentFile(b'PNGDATA'), save=True)
    response = auth_client.get(reverse('check-result-evidence', args=[result.pk]))
    assert response.status_code == 200
    assert response['Content-Type'] == 'image/png'
    assert response['Content-Disposition'].startswith('inline;')
    assert b''.join(response.streaming_content) == b'PNGDATA'
    assert result.screenshot.name not in response['Content-Disposition']


def test_foreign_or_missing_evidence_returns_404(auth_client, other_monitor, monitor):
    foreign = CheckResult.objects.create(monitor=other_monitor, is_success=False)
    missing = CheckResult.objects.create(monitor=monitor, is_success=False)
    assert auth_client.get(reverse('check-result-evidence', args=[foreign.pk])).status_code == 404
    assert auth_client.get(reverse('check-result-evidence', args=[missing.pk])).status_code == 404
