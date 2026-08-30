"""Validated, IP-pinned HTTP fetching.

One implementation, shared by the HTTP monitor and by the browser's request
gate. Two separate SSRF implementations would inevitably drift apart, and the
weaker one would become the way in.

The guarantee this module provides: the address that was validated is the
address that gets connected to. Every hop of a redirect chain is resolved and
checked before any packet is sent to it, the connection is opened against the
literal IP that passed, and the original hostname is still presented in the Host
header and as SNI, so certificate verification is unaffected.
"""

from dataclasses import dataclass

import httpx

from monitors.ssrf import ResolvedTarget, resolve_target

MAX_REDIRECTS = 5

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Redirects that must be replayed with the original method and body. The rest
# degrade to GET, which is what a browser and a plain HTTP client both do.
METHOD_PRESERVING_REDIRECTS = frozenset({307, 308})

# Headers we set ourselves or that describe a connection we are replacing.
# Forwarding these from an intercepted request would either lie to the origin or
# be silently overwritten.
SKIPPED_REQUEST_HEADERS = frozenset(
    {
        'host',
        'content-length',
        'connection',
        'keep-alive',
        'proxy-authorization',
        'proxy-connection',
        'te',
        'trailer',
        'transfer-encoding',
        'upgrade',
        # httpx negotiates and transparently decodes its own encoding.
        'accept-encoding',
    }
)


class TooManyRedirectsError(Exception):
    """The redirect chain did not terminate within the allowed number of hops."""


@dataclass(frozen=True)
class FetchResult:
    """A completed fetch, plus where it ended up and what it connected to."""

    response: httpx.Response
    target: ResolvedTarget
    final_url: str


def forwardable_headers(headers):
    """Strip the headers we must own from a set we are relaying."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in SKIPPED_REQUEST_HEADERS
    }


def build_pinned_request(method, url, target, headers=None, content=None):
    """Build a request aimed at the validated IP but addressed to the hostname.

    The URL host is swapped for the pinned address so no second DNS lookup can
    happen between validation and connection. The original host is restored in
    the Host header, and passed as SNI so certificate verification still runs
    against the real hostname rather than the bare IP.
    """
    pinned = httpx.URL(url).copy_with(host=target.ip, port=target.port)
    merged = dict(headers or {})
    merged['Host'] = target.host_header
    extensions = {'sni_hostname': target.hostname} if target.is_tls else {}
    return httpx.Request(method, pinned, headers=merged, content=content, extensions=extensions)


def fetch_validated(
    client,
    url,
    method='GET',
    headers=None,
    content=None,
    max_redirects=MAX_REDIRECTS,
):
    """Fetch `url`, following redirects one validated hop at a time.

    Raises BlockedTargetError or TargetResolutionError from the hop that failed,
    before that hop is contacted, and TooManyRedirectsError if the chain never
    settles.
    """
    current_url = url
    current_method = method
    current_content = content

    for _ in range(max_redirects + 1):
        # Resolution and validation happen here, and the request built from this
        # target can only reach the address that just passed.
        target = resolve_target(current_url)
        request = build_pinned_request(
            current_method, current_url, target, headers=headers, content=current_content
        )
        response = client.send(request)

        location = response.headers.get('location')
        if response.status_code in REDIRECT_STATUSES and location:
            current_url = str(httpx.URL(current_url).join(location))
            if response.status_code not in METHOD_PRESERVING_REDIRECTS:
                current_method = 'GET'
                current_content = None
            continue

        return FetchResult(response=response, target=target, final_url=current_url)

    raise TooManyRedirectsError(f'Exceeded {max_redirects} redirects starting from {url}.')
