import pytest
from django.urls import reverse

from websites.models import Website

pytestmark = pytest.mark.django_db

LIST_URL = 'website-list'
DETAIL_URL = 'website-detail'


def payload(**overrides):
    data = {'name': 'Marketing site', 'url': 'https://example.com/pricing'}
    data.update(overrides)
    return data


def make_website(owner, name='Their site', url='https://theirs.example.com'):
    return Website.objects.create(owner=owner, name=name, url=url)


def test_authenticated_user_can_create_website(auth_client, user):
    response = auth_client.post(reverse(LIST_URL), payload(), format='json')

    assert response.status_code == 201
    website = Website.objects.get(pk=response.json()['id'])
    assert website.owner == user
    assert website.url == 'https://example.com/pricing'


def test_unauthenticated_user_cannot_create_website(api_client):
    response = api_client.post(reverse(LIST_URL), payload(), format='json')

    assert response.status_code == 401
    assert not Website.objects.exists()


def test_owner_is_taken_from_request_not_payload(auth_client, user, other_user):
    response = auth_client.post(reverse(LIST_URL), payload(owner=other_user.pk), format='json')

    assert response.status_code == 201
    assert Website.objects.get(pk=response.json()['id']).owner == user


def test_user_can_list_own_websites(auth_client, user):
    make_website(user, name='Mine')

    response = auth_client.get(reverse(LIST_URL))

    assert response.status_code == 200
    assert [item['name'] for item in response.json()] == ['Mine']


def test_list_excludes_other_users_websites(auth_client, user, other_user):
    make_website(user, name='Mine')
    make_website(other_user, name='Theirs')

    response = auth_client.get(reverse(LIST_URL))

    assert response.status_code == 200
    assert [item['name'] for item in response.json()] == ['Mine']


def test_user_can_retrieve_own_website(auth_client, user):
    website = make_website(user, name='Mine')

    response = auth_client.get(reverse(DETAIL_URL, args=[website.pk]))

    assert response.status_code == 200
    assert response.json()['name'] == 'Mine'


def test_user_cannot_retrieve_another_users_website(auth_client, other_user):
    website = make_website(other_user)

    response = auth_client.get(reverse(DETAIL_URL, args=[website.pk]))

    assert response.status_code == 404


def test_user_can_update_own_website(auth_client, user):
    website = make_website(user, name='Mine')

    response = auth_client.patch(
        reverse(DETAIL_URL, args=[website.pk]), {'name': 'Renamed'}, format='json'
    )

    assert response.status_code == 200
    website.refresh_from_db()
    assert website.name == 'Renamed'


def test_user_cannot_update_another_users_website(auth_client, other_user):
    website = make_website(other_user, name='Theirs')

    response = auth_client.patch(
        reverse(DETAIL_URL, args=[website.pk]), {'name': 'Hijacked'}, format='json'
    )

    assert response.status_code == 404
    website.refresh_from_db()
    assert website.name == 'Theirs'


def test_user_can_delete_own_website(auth_client, user):
    website = make_website(user)

    response = auth_client.delete(reverse(DETAIL_URL, args=[website.pk]))

    assert response.status_code == 204
    assert not Website.objects.filter(pk=website.pk).exists()


def test_user_cannot_delete_another_users_website(auth_client, other_user):
    website = make_website(other_user)

    response = auth_client.delete(reverse(DETAIL_URL, args=[website.pk]))

    assert response.status_code == 404
    assert Website.objects.filter(pk=website.pk).exists()


@pytest.mark.parametrize('url', ['not a url', 'ftp://example.com', 'https://', ''])
def test_invalid_url_is_rejected(auth_client, url):
    response = auth_client.post(reverse(LIST_URL), payload(url=url), format='json')

    assert response.status_code == 400
    assert 'url' in response.json()
    assert not Website.objects.exists()


@pytest.mark.parametrize('name', ['', '   '])
def test_empty_name_is_rejected(auth_client, name):
    response = auth_client.post(reverse(LIST_URL), payload(name=name), format='json')

    assert response.status_code == 400
    assert 'name' in response.json()
    assert not Website.objects.exists()


@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        ('example.com', 'https://example.com'),
        ('example.com/', 'https://example.com'),
        ('HTTP://Example.COM/Status', 'http://example.com/Status'),
        ('https://example.com/health?deep=1', 'https://example.com/health?deep=1'),
    ],
)
def test_url_is_normalized_without_losing_meaningful_parts(auth_client, given, expected):
    response = auth_client.post(reverse(LIST_URL), payload(url=given), format='json')

    assert response.status_code == 201
    assert response.json()['url'] == expected
