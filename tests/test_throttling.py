"""Rate limiting is enforced by DRF throttles backed by the Django cache.

Every test here relies on the autouse cache-clearing fixture in conftest, so
each one starts from an empty set of throttle counters.
"""

import pytest
from django.urls import reverse

from tests.conftest import PASSWORD
from websites.models import Website

pytestmark = pytest.mark.django_db

LOGIN_RATE = 5
REGISTER_RATE = 5
REFRESH_RATE = 20


def test_login_is_throttled_after_the_configured_limit(api_client, user):
    credentials = {'email': user.email, 'password': PASSWORD}

    for _ in range(LOGIN_RATE):
        allowed = api_client.post(reverse('login'), credentials, format='json')
        assert allowed.status_code == 200

    throttled = api_client.post(reverse('login'), credentials, format='json')

    assert throttled.status_code == 429


def test_failed_logins_also_count_towards_the_limit(api_client, user):
    """Throttling runs before the view, so guessing passwords is limited too."""
    wrong = {'email': user.email, 'password': 'not-the-password'}

    for _ in range(LOGIN_RATE):
        assert api_client.post(reverse('login'), wrong, format='json').status_code == 401

    throttled = api_client.post(reverse('login'), wrong, format='json')

    assert throttled.status_code == 429


def test_registration_is_throttled(api_client):
    for index in range(REGISTER_RATE):
        payload = {'email': f'signup{index}@example.com', 'password': PASSWORD}
        allowed = api_client.post(reverse('register'), payload, format='json')
        assert allowed.status_code == 201

    throttled = api_client.post(
        reverse('register'),
        {'email': 'one-too-many@example.com', 'password': PASSWORD},
        format='json',
    )

    assert throttled.status_code == 429


def test_token_refresh_is_throttled(api_client, user):
    login = api_client.post(
        reverse('login'), {'email': user.email, 'password': PASSWORD}, format='json'
    )
    refresh = login.json()['refresh']

    # The first request spends the only valid refresh token; the rest are
    # rejected as invalid but still consume the rate limit.
    first = api_client.post(reverse('token-refresh'), {'refresh': refresh}, format='json')
    assert first.status_code == 200

    for _ in range(REFRESH_RATE - 1):
        repeat = api_client.post(reverse('token-refresh'), {'refresh': refresh}, format='json')
        assert repeat.status_code == 401

    throttled = api_client.post(reverse('token-refresh'), {'refresh': refresh}, format='json')

    assert throttled.status_code == 429


def test_login_and_register_limits_are_independent(api_client, user):
    """Separate scopes, so exhausting one must not lock out the other."""
    for _ in range(LOGIN_RATE + 1):
        api_client.post(
            reverse('login'), {'email': user.email, 'password': PASSWORD}, format='json'
        )

    response = api_client.post(
        reverse('register'),
        {'email': 'still-allowed@example.com', 'password': PASSWORD},
        format='json',
    )

    assert response.status_code == 201


def test_website_crud_is_unaffected_within_the_allowed_rate(auth_client, user):
    """Well under the 120/minute authenticated ceiling, nothing is throttled."""
    for index in range(10):
        created = auth_client.post(
            reverse('website-list'),
            {'name': f'Site {index}', 'url': f'https://example{index}.com/status'},
            format='json',
        )
        assert created.status_code == 201

        listed = auth_client.get(reverse('website-list'))
        assert listed.status_code == 200

        detail_url = reverse('website-detail', args=[created.json()['id']])
        assert auth_client.patch(detail_url, {'name': 'Renamed'}, format='json').status_code == 200

    assert Website.objects.filter(owner=user).count() == 10


def test_health_endpoint_is_not_throttled(api_client):
    for _ in range(25):
        assert api_client.get(reverse('health')).status_code == 200
