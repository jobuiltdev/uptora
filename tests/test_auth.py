import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from tests.conftest import PASSWORD

User = get_user_model()

pytestmark = pytest.mark.django_db


def register(client, **overrides):
    payload = {'email': 'new@example.com', 'password': PASSWORD}
    payload.update(overrides)
    return client.post(reverse('register'), payload, format='json')


def login(client, email='owner@example.com', password=PASSWORD):
    credentials = {'email': email, 'password': password}
    return client.post(reverse('login'), credentials, format='json')


def test_registration_succeeds(api_client):
    response = register(api_client)

    assert response.status_code == 201
    assert response.json()['email'] == 'new@example.com'
    assert 'password' not in response.json()
    assert User.objects.filter(email='new@example.com').exists()


def test_registration_normalizes_email(api_client):
    response = register(api_client, email='  New@Example.COM  ')

    assert response.status_code == 201
    assert response.json()['email'] == 'new@example.com'


def test_duplicate_email_registration_fails(api_client, user):
    response = register(api_client, email='OWNER@example.com')

    assert response.status_code == 400
    assert 'email' in response.json()
    assert User.objects.filter(email='owner@example.com').count() == 1


def test_registration_rejects_weak_password(api_client):
    response = register(api_client, password='123')

    assert response.status_code == 400
    assert 'password' in response.json()


def test_login_succeeds(api_client, user):
    response = login(api_client)

    assert response.status_code == 200
    assert 'access' in response.json()
    assert 'refresh' in response.json()


def test_login_with_invalid_password_fails(api_client, user):
    response = login(api_client, password='not-the-password')

    assert response.status_code == 401
    assert 'access' not in response.json()


def test_me_returns_authenticated_user(auth_client, user):
    response = auth_client.get(reverse('me'))

    assert response.status_code == 200
    body = response.json()
    assert body['email'] == user.email
    assert 'password' not in body


def test_me_requires_authentication(api_client):
    response = api_client.get(reverse('me'))

    assert response.status_code == 401


def test_refresh_token_returns_new_access_token(api_client, user):
    refresh = login(api_client).json()['refresh']

    response = api_client.post(reverse('token-refresh'), {'refresh': refresh}, format='json')

    assert response.status_code == 200
    assert 'access' in response.json()


def test_logout_invalidates_refresh_token(api_client, user):
    tokens = login(api_client).json()
    api_client.credentials(HTTP_AUTHORIZATION='Bearer ' + tokens['access'])

    logout = api_client.post(reverse('logout'), {'refresh': tokens['refresh']}, format='json')
    assert logout.status_code == 204

    api_client.credentials()
    refreshed = api_client.post(
        reverse('token-refresh'), {'refresh': tokens['refresh']}, format='json'
    )
    assert refreshed.status_code == 401
