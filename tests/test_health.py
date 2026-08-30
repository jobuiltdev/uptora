from django.urls import reverse


def test_health_endpoint_returns_200(client):
    response = client.get(reverse('health'))

    assert response.status_code == 200


def test_health_endpoint_returns_expected_payload(client):
    response = client.get(reverse('health'))

    assert response.json() == {'status': 'ok'}
