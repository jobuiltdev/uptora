"""Target validation. Users control the URLs Uptora fetches, so this module is
what stops Uptora from being turned into a proxy into private networks.

The rules are enforced here, immediately before every request, rather than when
a website is saved. DNS is mutable: a hostname that resolved to a public address
last week can resolve to 10.0.0.5 today, so a write-time check proves nothing.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({'http', 'https'})

DEFAULT_PORTS = {'http': 80, 'https': 443}

# Resolution should catch these anyway, but naming them gives a clear error and
# covers resolvers configured to do something surprising.
BLOCKED_HOSTNAMES = frozenset(
    {
        'localhost',
        'localhost.localdomain',
        'ip6-localhost',
        'ip6-loopback',
    }
)


class BlockedTargetError(Exception):
    """The target is not allowed to be fetched."""


class TargetResolutionError(Exception):
    """The hostname could not be resolved."""


@dataclass(frozen=True)
class ResolvedTarget:
    """A target that passed validation, with the address to connect to.

    `ip` is pinned: the caller connects to this address rather than handing the
    hostname to the HTTP client, which would resolve a second time and could get
    a different answer.
    """

    scheme: str
    hostname: str
    port: int
    ip: str

    @property
    def host_header(self):
        """The Host header the origin expects, since the URL carries a raw IP."""
        host = f'[{self.hostname}]' if ':' in self.hostname else self.hostname
        if self.port == DEFAULT_PORTS[self.scheme]:
            return host
        return f'{host}:{self.port}'

    @property
    def is_tls(self):
        return self.scheme == 'https'


def ensure_public_ip(value):
    """Raise BlockedTargetError unless `value` is a routable public address."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise BlockedTargetError(f'{value!r} is not a valid IP address.') from exc

    # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if ip.is_unspecified:
        raise BlockedTargetError(f'{ip} is an unspecified address.')
    if ip.is_loopback:
        raise BlockedTargetError(f'{ip} is a loopback address.')
    if ip.is_link_local:
        raise BlockedTargetError(f'{ip} is a link-local address.')
    if ip.is_multicast:
        raise BlockedTargetError(f'{ip} is a multicast address.')
    if ip.is_reserved:
        raise BlockedTargetError(f'{ip} is a reserved address.')
    if ip.is_private:
        raise BlockedTargetError(f'{ip} is a private address.')
    # Catch-all for anything the named checks miss, such as carrier-grade NAT.
    if not ip.is_global:
        raise BlockedTargetError(f'{ip} is not a public address.')

    return ip


def resolve_addresses(hostname, port):
    """Return every address `hostname` resolves to, as strings."""
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TargetResolutionError(f'Could not resolve {hostname!r}.') from exc

    addresses = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)

    if not addresses:
        raise TargetResolutionError(f'Could not resolve {hostname!r}.')
    return addresses


def resolve_target(url):
    """Validate `url` and return the address to connect to.

    Raises BlockedTargetError if anything about the target is disallowed, or
    TargetResolutionError if the hostname does not resolve.
    """
    parts = urlsplit(url)

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedTargetError(f'Scheme {scheme or "(none)"!r} is not supported.')

    # `.hostname` already strips any user:password@ prefix, so
    # http://example.com@127.0.0.1/ is read as the host 127.0.0.1.
    hostname = (parts.hostname or '').strip().rstrip('.').lower()
    if not hostname:
        raise BlockedTargetError('The URL has no hostname.')
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith('.localhost'):
        raise BlockedTargetError(f'{hostname!r} is not a public host.')

    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise BlockedTargetError('The URL has an invalid port.') from exc

    addresses = resolve_addresses(hostname, port)

    # Every answer has to pass. A host that returns one public and one private
    # address is trying to slip past a check that only looks at the first.
    for address in addresses:
        ensure_public_ip(address)

    return ResolvedTarget(scheme=scheme, hostname=hostname, port=port, ip=addresses[0])
