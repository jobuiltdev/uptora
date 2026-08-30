import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from monitors.models import Monitor
from websites.models import Website

User = get_user_model()

PASSWORD = 'uptora-test-pass-42'


def pytest_configure():
    """Test-only overrides. config.settings is untouched, so dev and production
    keep Django's real password hashers."""
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    """Throttle counters live in the cache, which outlives a single test.
    Clearing around every test keeps rate limits from leaking between them."""
    cache.clear()
    yield
    cache.clear()


def bearer_client(user):
    client = APIClient()
    access = RefreshToken.for_user(user).access_token
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
    return client


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(email='owner@example.com', password=PASSWORD)


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email='other@example.com', password=PASSWORD)


@pytest.fixture
def auth_client(user):
    return bearer_client(user)


@pytest.fixture
def other_client(other_user):
    return bearer_client(other_user)


@pytest.fixture
def website(user):
    return Website.objects.create(owner=user, name='Example', url='https://example.com/status')


@pytest.fixture
def other_website(other_user):
    return Website.objects.create(owner=other_user, name='Theirs', url='https://theirs.example.com')


@pytest.fixture
def monitor(website):
    return Monitor.objects.create(website=website)


@pytest.fixture
def other_monitor(other_website):
    return Monitor.objects.create(website=other_website)
