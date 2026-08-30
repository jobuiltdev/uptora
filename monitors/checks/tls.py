"""Certificate inspection.

Kept separate from the request path on purpose: reading certificate expiry is
metadata collection, not an availability signal. See inspect_certificate for the
reasoning about failures.
"""

import socket
import ssl
from datetime import UTC, datetime

# The format OpenSSL hands back in notAfter, e.g. 'Jun  1 12:00:00 2027 GMT'.
NOT_AFTER_FORMAT = '%b %d %H:%M:%S %Y %Z'


def parse_not_after(value):
    """Parse an OpenSSL notAfter string into an aware UTC datetime."""
    return datetime.strptime(value, NOT_AFTER_FORMAT).replace(tzinfo=UTC)


def inspect_certificate(hostname, ip, port, timeout_seconds):
    """Return the certificate expiry for a target, or None if it cannot be read.

    Returning None rather than raising is deliberate. This runs only after the
    HTTP request already succeeded, which means TLS verification already passed;
    a failure here is a problem with our extra connection, not with the site.
    Turning that into a failed check would raise a false alarm about a site that
    is demonstrably up.

    Connects to the pinned `ip` while presenting `hostname` for SNI, matching
    how the request itself was made.
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ip, port), timeout=timeout_seconds) as raw:
            with context.wrap_socket(raw, server_hostname=hostname) as tls:
                certificate = tls.getpeercert()
    except (OSError, ssl.SSLError, ValueError):
        return None

    not_after = (certificate or {}).get('notAfter')
    if not not_after:
        return None
    try:
        return parse_not_after(not_after)
    except ValueError:
        return None


def days_remaining(expires_at, now):
    """Whole days until `expires_at`. Negative once the certificate has expired."""
    return (expires_at - now).days
