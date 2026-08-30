"""SSRF gate tests.

DNS is stubbed everywhere, so none of this touches a real resolver or network.
"""

import socket

import pytest

from monitors import ssrf
from monitors.ssrf import (
    BlockedTargetError,
    TargetResolutionError,
    ensure_public_ip,
    resolve_target,
)


@pytest.fixture
def stub_dns(monkeypatch):
    """Point every hostname at a chosen list of addresses."""

    def configure(*addresses):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (
                    socket.AF_INET6 if ':' in address else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    '',
                    (address, port),
                )
                for address in addresses
            ]

        monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)

    return configure


@pytest.fixture
def literal_dns(monkeypatch):
    """Resolve any host to itself, for testing IP literals in URLs."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (host, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)


PRIVATE_ADDRESSES = [
    pytest.param('127.0.0.1', id='ipv4-loopback'),
    pytest.param('10.1.2.3', id='private-10'),
    pytest.param('172.16.0.1', id='private-172-16'),
    pytest.param('172.31.255.254', id='private-172-31'),
    pytest.param('192.168.1.1', id='private-192-168'),
    pytest.param('169.254.169.254', id='link-local-metadata'),
    pytest.param('0.0.0.0', id='unspecified'),
    pytest.param('224.0.0.1', id='multicast'),
    pytest.param('::1', id='ipv6-loopback'),
    pytest.param('fd00::1', id='ipv6-unique-local'),
    pytest.param('fe80::1', id='ipv6-link-local'),
    pytest.param('::ffff:127.0.0.1', id='ipv4-mapped-loopback'),
]


@pytest.mark.parametrize('address', PRIVATE_ADDRESSES)
def test_ensure_public_ip_rejects_non_public_addresses(address):
    with pytest.raises(BlockedTargetError):
        ensure_public_ip(address)


def test_ensure_public_ip_allows_a_public_address():
    assert str(ensure_public_ip('93.184.216.34')) == '93.184.216.34'


@pytest.mark.parametrize('address', PRIVATE_ADDRESSES)
def test_url_resolving_to_a_private_address_is_rejected(address, stub_dns):
    stub_dns(address)

    with pytest.raises(BlockedTargetError):
        resolve_target('https://sneaky.example.com/')


@pytest.mark.parametrize(
    'url',
    [
        'http://127.0.0.1/',
        'http://10.0.0.5/',
        'http://172.20.0.1/',
        'http://192.168.0.10/',
        'http://169.254.169.254/latest/meta-data/',
        'http://[::1]/',
        'http://[fd00::1]/',
    ],
)
def test_ip_literals_pointing_inside_the_network_are_rejected(url, literal_dns):
    with pytest.raises(BlockedTargetError):
        resolve_target(url)


@pytest.mark.parametrize(
    'hostname',
    ['localhost', 'LOCALHOST', 'localhost.', 'localhost.localdomain', 'anything.localhost'],
)
def test_localhost_hostnames_are_rejected_before_resolution(hostname, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError('resolution should not be attempted for a blocked hostname')

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', explode)

    with pytest.raises(BlockedTargetError):
        resolve_target(f'http://{hostname}/')


def test_credentials_in_the_url_cannot_disguise_the_real_host(literal_dns):
    """http://example.com@127.0.0.1/ actually addresses 127.0.0.1."""
    with pytest.raises(BlockedTargetError):
        resolve_target('http://example.com@127.0.0.1/')


def test_decimal_encoded_loopback_is_rejected(stub_dns):
    """http://2130706433/ is 127.0.0.1 written as an integer."""
    stub_dns('127.0.0.1')

    with pytest.raises(BlockedTargetError):
        resolve_target('http://2130706433/')


def test_a_single_private_answer_blocks_a_host_with_public_answers_too(stub_dns):
    """Mixed answers are a rebinding attempt, not a partially valid target."""
    stub_dns('93.184.216.34', '10.0.0.5')

    with pytest.raises(BlockedTargetError):
        resolve_target('https://mixed.example.com/')


@pytest.mark.parametrize('scheme', ['ftp', 'file', 'gopher'])
def test_unsupported_schemes_are_rejected(scheme, literal_dns):
    with pytest.raises(BlockedTargetError):
        resolve_target(f'{scheme}://example.com/')


def test_unresolvable_hostname_raises_a_resolution_error(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror('Name or service not known')

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fail)

    with pytest.raises(TargetResolutionError):
        resolve_target('https://nope.example.com/')


def test_public_target_is_allowed_and_pinned(stub_dns):
    stub_dns('93.184.216.34')

    target = resolve_target('https://example.com/status')

    assert target.scheme == 'https'
    assert target.hostname == 'example.com'
    assert target.port == 443
    assert target.ip == '93.184.216.34'
    assert target.is_tls


def test_host_header_omits_the_default_port_but_keeps_a_custom_one(stub_dns):
    stub_dns('93.184.216.34')

    assert resolve_target('https://example.com/').host_header == 'example.com'
    assert resolve_target('https://example.com:8443/').host_header == 'example.com:8443'
