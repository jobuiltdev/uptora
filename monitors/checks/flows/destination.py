"""Where a flow is allowed to submit.

SSRF protection answers "may Uptora connect to this address at all". This
answers a different question: "is this the customer's own site". Both have to
hold for a write, and the address question is settled first so a private target
is still reported as one.

The threat is spam relaying rather than internal access. Without this rule a
user could point a monitor at a page they control, put a form on it aimed at an
unrelated public site, and have Uptora POST a payload of their choosing to that
third party on a schedule. Uptora would be a request sender wearing a
monitoring badge.

Policy: same origin, plus one exception
---------------------------------------
An origin is (scheme, host, effective port), compared exactly:

  * scheme must match. https may not submit to http: that would hand a message
    to the network in cleartext, and it is a downgrade the monitored page can
    choose unilaterally.
  * effective port must match, with the scheme default filled in first, so
    https://example.com and https://example.com:443 are the same origin and
    :8443 is not.
  * host must match, except that a host may also match its own www form:
    www.example.com and example.com submit to each other.

The www exception compares a host only against itself with one leading `www.`
label removed. It never widens a host to a parent domain, so there is nothing
here that needs to know which suffixes are registrable, and no approximation of
the public suffix list to get wrong. `forms.example.com` and `example.com` are
simply different hosts, and so are `shop.example.com` and `example.com`.

That refuses legitimate cases -- sibling subdomains, and third-party form
providers such as Formspree or HubSpot. For this milestone that is the intended
trade: a rule that is exactly right about a narrower set beats one that is
approximately right about a wider one when it is the boundary stopping Uptora
from becoming a spam relay.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from monitors.ssrf import DEFAULT_PORTS


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int


def parse_origin(url):
    """The origin of `url`, or None when it does not have a usable one.

    Anything unparseable, portless-but-unknown-scheme, or hostless is None,
    which every caller treats as "refuse". Normalisation is limited to what the
    URL grammar already guarantees: lowercasing, and dropping a single trailing
    root dot. Nothing is stripped that could change which host is meant.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        return None

    host = (parts.hostname or '').strip().lower().rstrip('.')
    if not host:
        return None

    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError:
        return None

    return Origin(scheme=scheme, host=host, port=port)


def without_www(host):
    """The host with a single leading `www.` label removed.

    Only ever used to compare a host with its own www form. It is never used to
    derive a parent domain, so `www.com` collapsing to `com` cannot widen
    anything: `com` still only matches `com` or `www.com`, never `evil.com`.
    """
    return host[4:] if host.startswith('www.') else host


def same_origin(destination_url, monitored_url):
    """Whether a submission destination is the monitored site's own origin."""
    destination = parse_origin(destination_url)
    monitored = parse_origin(monitored_url)
    if destination is None or monitored is None:
        return False

    if destination.scheme != monitored.scheme or destination.port != monitored.port:
        return False

    return without_www(destination.host) == without_www(monitored.host)


def describe(origin):
    """A readable origin for an error message."""
    if origin is None:
        return 'an unusable destination'
    if origin.port == DEFAULT_PORTS[origin.scheme]:
        return f'{origin.scheme}://{origin.host}'
    return f'{origin.scheme}://{origin.host}:{origin.port}'


def build_submission_policy(monitored_url):
    """A callable the browser session consults before sending a submission.

    Returns (allowed, reason). Only ever asked about requests attributable to
    the configured submit action; ordinary subresources are none of its
    business.
    """
    monitored = parse_origin(monitored_url)

    def allowed(url):
        if same_origin(url, monitored_url):
            return True, None
        return False, (
            f'{describe(parse_origin(url))} is not the origin of the monitored site '
            f'({describe(monitored)}). A contact form may only submit to its own origin, '
            f'or between its apex and www host.'
        )

    return allowed
