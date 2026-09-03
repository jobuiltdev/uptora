"""Deployment boundaries without provisioning a provider or contacting S3."""

import json

from django.conf import settings
from django.core.files.storage import storages
from django.core.management import call_command
from django.test import override_settings
from storages.backends.s3 import S3Storage


@override_settings(SECURE_SSL_REDIRECT=True)
def test_container_http_liveness_works_but_other_routes_still_require_https(client):
    response = client.get('/api/health/')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
    for path in ('/api/readiness/', '/api/auth/registration-status/', '/admin/'):
        response = client.get(path)
        assert response.status_code == 301
        assert response['Location'].startswith('https://')


@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'),
)
def test_trusted_https_proxy_does_not_redirect_loop(client):
    response = client.get('/api/auth/registration-status/', HTTP_X_FORWARDED_PROTO='https')
    assert response.status_code == 200


def test_collected_admin_static_is_served_without_exposing_media(client, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    # A synthetic file, not actual monitoring evidence.
    (media_root / 'private-evidence.png').touch()
    with override_settings(
        DEBUG=False,
        SECURE_SSL_REDIRECT=False,
        STATIC_ROOT=tmp_path / 'static',
        MEDIA_ROOT=media_root,
        WHITENOISE_AUTOREFRESH=False,
    ):
        call_command('collectstatic', interactive=False, verbosity=0)
        response = client.get('/static/admin/css/base.css')
        assert response.status_code == 200
        assert 'text/css' in response['Content-Type']
        assert b'body' in b''.join(response.streaming_content)
        assert client.get('/media/private-evidence.png').status_code == 404


def test_generic_storage_options_load_s3_adapter_without_public_acl():
    options = json.loads(
        '{"bucket_name":"test-evidence", "endpoint_url":"https://s3.example.invalid",'
        '"region_name":"test-region", "default_acl":null, "querystring_auth":true,'
        '"file_overwrite":false, "access_key":"test-only", "secret_key":"test-only"}'
    )
    with override_settings(
        STORAGES={
            **settings.STORAGES,
            'default': {'BACKEND': 'storages.backends.s3.S3Storage', 'OPTIONS': options},
        }
    ):
        storage = storages['default']
        assert isinstance(storage, S3Storage)
        assert storage.bucket_name == 'test-evidence'
        assert storage.endpoint_url == 'https://s3.example.invalid'
        assert storage.default_acl is None
        assert storage.querystring_auth is True
        assert storage.file_overwrite is False
