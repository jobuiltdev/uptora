"""The shared validated, IP-pinned fetch path.

One implementation backs both the HTTP monitor and the browser gate, so this is
where the "validation controls the connection" guarantee is pinned down.
"""

import socket

import httpx
import pytest

from monitors import ssrf
from monitors.checks.fetching import (
    TooManyRedirectsError,
    fetch_validated,
    forwardable_headers,
)
from monitors.ssrf import BlockedTargetError, TargetResolutionError

PUBLIC_IP = '93.184.216.34'
OTHER_PUBLIC_IP = '93.184.216.35'


@pytest.fixture
def dns(monkeypatch):
    """Map hostnames to chosen addresses; IP literals resolve to themselves."""
    table = {}

    def configure(**hosts):
        table.update(hosts)

    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = table.get(host, host)
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (address, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    return configure


def client_recording(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped), follow_redirects=False), seen


class TestPinning:
    def test_the_connection_goes_to_the_validated_address(self, dns):
        dns(**{'example.com': PUBLIC_IP})
        client, seen = client_recording(lambda request: httpx.Response(200))

        with client:
            fetch_validated(client, 'https://example.com/page')

        assert seen[0].url.host == PUBLIC_IP
        assert seen[0].headers['host'] == 'example.com'
        assert seen[0].extensions['sni_hostname'] == 'example.com'

    def test_a_non_default_port_is_preserved_in_the_host_header(self, dns):
        dns(**{'example.com': PUBLIC_IP})
        client, seen = client_recording(lambda request: httpx.Response(200))

        with client:
            fetch_validated(client, 'https://example.com:8443/page')

        assert seen[0].url.host == PUBLIC_IP
        assert seen[0].url.port == 8443
        assert seen[0].headers['host'] == 'example.com:8443'

    def test_plain_http_carries_no_sni(self, dns):
        dns(**{'example.com': PUBLIC_IP})
        client, seen = client_recording(lambda request: httpx.Response(200))

        with client:
            fetch_validated(client, 'http://example.com/page')

        assert 'sni_hostname' not in seen[0].extensions


class TestRedirectValidation:
    def test_every_hop_is_pinned(self, dns):
        dns(**{'example.com': PUBLIC_IP, 'elsewhere.example': OTHER_PUBLIC_IP})

        def handler(request):
            if request.url.path == '/start':
                return httpx.Response(302, headers={'location': 'https://elsewhere.example/end'})
            return httpx.Response(200)

        client, seen = client_recording(handler)
        with client:
            result = fetch_validated(client, 'https://example.com/start')

        assert [request.url.host for request in seen] == [PUBLIC_IP, OTHER_PUBLIC_IP]
        assert [request.headers['host'] for request in seen] == [
            'example.com',
            'elsewhere.example',
        ]
        assert result.final_url == 'https://elsewhere.example/end'

    def test_a_private_hop_is_refused_before_it_is_contacted(self, dns):
        dns(**{'example.com': PUBLIC_IP, 'internal.example': '10.0.0.5'})

        def handler(request):
            if request.url.path == '/start':
                return httpx.Response(302, headers={'location': 'http://internal.example/secret'})
            raise AssertionError('the private hop must never be requested')

        client, seen = client_recording(handler)
        with client, pytest.raises(BlockedTargetError):
            fetch_validated(client, 'https://example.com/start')

        # Only the first, public hop was ever sent.
        assert len(seen) == 1
        assert seen[0].url.host == PUBLIC_IP

    def test_a_multi_hop_chain_stops_at_the_private_hop(self, dns):
        dns(
            **{
                'example.com': PUBLIC_IP,
                'second.example': OTHER_PUBLIC_IP,
                'internal.example': '169.254.169.254',
            }
        )
        chain = {
            '/one': 'https://second.example/two',
            '/two': 'http://internal.example/three',
        }

        def handler(request):
            location = chain.get(request.url.path)
            if location:
                return httpx.Response(302, headers={'location': location})
            raise AssertionError('the private hop must never be requested')

        client, seen = client_recording(handler)
        with client, pytest.raises(BlockedTargetError):
            fetch_validated(client, 'https://example.com/one')

        assert len(seen) == 2

    def test_a_redirect_to_a_literal_private_address_is_refused(self, dns):
        dns(**{'example.com': PUBLIC_IP})

        def handler(request):
            if request.url.path == '/start':
                return httpx.Response(302, headers={'location': 'http://127.0.0.1:8080/x'})
            raise AssertionError('loopback must never be requested')

        client, _ = client_recording(handler)
        with client, pytest.raises(BlockedTargetError):
            fetch_validated(client, 'https://example.com/start')

    def test_an_unresolvable_hop_is_reported_as_such(self, dns, monkeypatch):
        dns(**{'example.com': PUBLIC_IP})
        calls = {'n': 0}
        real = ssrf.socket.getaddrinfo

        def flaky(host, port, *args, **kwargs):
            calls['n'] += 1
            if host == 'gone.example':
                raise socket.gaierror('Name or service not known')
            return real(host, port, *args, **kwargs)

        monkeypatch.setattr(ssrf.socket, 'getaddrinfo', flaky)

        def handler(request):
            return httpx.Response(302, headers={'location': 'https://gone.example/x'})

        client, _ = client_recording(handler)
        with client, pytest.raises(TargetResolutionError):
            fetch_validated(client, 'https://example.com/start')

    def test_the_chain_length_is_capped(self, dns):
        dns(**{'example.com': PUBLIC_IP})
        client, seen = client_recording(
            lambda request: httpx.Response(302, headers={'location': '/again'})
        )

        with client, pytest.raises(TooManyRedirectsError):
            fetch_validated(client, 'https://example.com/start')

        assert len(seen) == 6

    def test_a_3xx_without_a_location_is_the_final_response(self, dns):
        dns(**{'example.com': PUBLIC_IP})
        client, _ = client_recording(lambda request: httpx.Response(301))

        with client:
            result = fetch_validated(client, 'https://example.com/start')

        assert result.response.status_code == 301


class TestMethodAndBody:
    def test_a_307_preserves_the_method_and_body(self, dns):
        dns(**{'example.com': PUBLIC_IP})

        def handler(request):
            if request.url.path == '/start':
                return httpx.Response(307, headers={'location': '/moved'})
            return httpx.Response(200)

        client, seen = client_recording(handler)
        with client:
            fetch_validated(client, 'https://example.com/start', method='POST', content=b'payload')

        assert [request.method for request in seen] == ['POST', 'POST']
        assert seen[1].read() == b'payload'

    def test_a_302_degrades_to_get(self, dns):
        dns(**{'example.com': PUBLIC_IP})

        def handler(request):
            if request.url.path == '/start':
                return httpx.Response(302, headers={'location': '/moved'})
            return httpx.Response(200)

        client, seen = client_recording(handler)
        with client:
            fetch_validated(client, 'https://example.com/start', method='POST', content=b'payload')

        assert [request.method for request in seen] == ['POST', 'GET']
        assert seen[1].read() == b''


class TestHeaderForwarding:
    def test_supplied_headers_reach_the_origin(self, dns):
        dns(**{'example.com': PUBLIC_IP})
        client, seen = client_recording(lambda request: httpx.Response(200))

        with client:
            fetch_validated(
                client,
                'https://example.com/page',
                headers=forwardable_headers({'User-Agent': 'Chrome', 'Host': 'forged.example'}),
            )

        assert seen[0].headers['user-agent'] == 'Chrome'
        # The forged Host was dropped and replaced by the validated one.
        assert seen[0].headers['host'] == 'example.com'
