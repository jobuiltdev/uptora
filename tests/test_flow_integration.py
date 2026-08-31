"""Contact-form flows against a real headless Chromium and a real local server.

Reuses the Milestone 4 harness idea: a fixture server serving the pages, and a
forbidden server that counts every TCP connection it should never receive. A
blocked write has to prove connections == 0, not merely that Uptora reported a
block.

This suite is where POST bodies, content types, redirect method semantics,
cookies and CSRF tokens are actually exercised, because all of those depend on
the real request pipeline rather than on a stub.
"""

import socket
import socketserver
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from monitors import ssrf
from monitors.checks.flow import run_flow_check
from monitors.checks.flows.contact_form import ContactFormPlan, FieldAction
from monitors.models import ErrorType, FlowFieldType

FIXTURE_IP = '127.0.0.1'

INTERNAL_HOST = 'internal.fixture.test'
METADATA_HOST = 'metadata.fixture.test'
SITE_HOST = 'site.fixture.test'
WWW_SITE_HOST = 'www.site.fixture.test'
UNRELATED_HOST = 'unrelated.fixture.test'
SIBLING_HOST = 'forms.site.fixture.test'

# Private for the gate; Chromium is pointed at the tripwire for these two.
STUBBED_DNS = {INTERNAL_HOST: '10.0.0.5', METADATA_HOST: '169.254.169.254'}

# Public as far as the gate is concerned, and genuinely reachable, so a policy
# failure would actually connect and be counted rather than merely time out.
PUBLIC_STUBBED_DNS = (SITE_HOST, WWW_SITE_HOST, UNRELATED_HOST, SIBLING_HOST)


def chromium_available():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


CHROMIUM = chromium_available()
SKIP_REASON = 'Chromium is not installed; run: playwright install chromium'

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not CHROMIUM, reason=SKIP_REASON),
]


class Tripwire:
    def __init__(self):
        self.connections = 0
        self._lock = threading.Lock()

    def record(self):
        with self._lock:
            self.connections += 1

    def reset(self):
        with self._lock:
            self.connections = 0


TRIPWIRE = Tripwire()


class CountingHandler(socketserver.StreamRequestHandler):
    def handle(self):
        TRIPWIRE.record()
        try:
            self.rfile.readline()
        except OSError:
            return
        body = b'this must never be reached'
        self.wfile.write(
            b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n%s'
            % (len(body), body)
        )


class Submissions:
    """What the fixture server actually received, for assertions."""

    def __init__(self):
        self.posts = []
        self._lock = threading.Lock()

    def record(self, entry):
        with self._lock:
            self.posts.append(entry)

    def reset(self):
        with self._lock:
            self.posts.clear()

    @property
    def last(self):
        return self.posts[-1] if self.posts else None


SUBMISSIONS = Submissions()

# Filled in by the forbidden_port fixture so pages can point at the tripwire.
TRIPWIRE_PORT = [0]

# Issued on the contact page, required back on submit. A stand-in for a real
# framework's CSRF protection: a token from the DOM plus a session cookie.
TOKENS = {}


def page(body):
    return (f'<!doctype html><html><body><h1>Contact us</h1>{body}</body></html>').encode()


def contact_form(action='/submit', method='post', extra='', hidden=''):
    return page(
        f'<form id="contact" action="{action}" method="{method}">'
        f'{hidden}'
        '<input id="name" name="name" type="text">'
        '<input id="email" name="email" type="email">'
        '<textarea id="message" name="message"></textarea>'
        '<input id="consent" name="consent" type="checkbox">'
        '<select id="topic" name="topic">'
        '<option value="sales">Sales</option><option value="support">Support</option>'
        '</select>'
        '<button id="send" type="submit">Send</button>'
        f'</form>{extra}'
    )


AJAX_FORM = page(
    '<form id="contact" onsubmit="return false;">'
    '<input id="name" name="name" type="text">'
    '<input id="email" name="email" type="email">'
    '<button id="send" type="button">Send</button>'
    '</form>'
    '<div id="outcome"></div>'
    '<script>'
    'document.getElementById("send").addEventListener("click", async () => {'
    '  const body = new URLSearchParams({name: document.getElementById("name").value});'
    '  const r = await fetch("/ajax-submit", {method: "POST", body});'
    '  const t = await r.text();'
    '  document.getElementById("outcome").innerHTML = '
    '    "<div class=\\"success-message\\">" + t + "</div>";'
    '});'
    '</script>'
)

PRIVATE_XHR_FORM = page(
    '<form id="contact"><input id="name" type="text">'
    '<button id="send" type="button">Send</button></form>'
    '<div id="outcome"></div>'
    '<script>'
    'document.getElementById("send").addEventListener("click", async () => {'
    '  try { await fetch("http://' + METADATA_HOST + '/latest/meta-data/"); } catch (e) {}'
    '  document.getElementById("outcome").textContent = "Thanks for your message";'
    '});'
    '</script>'
)


def ajax_form(endpoint, unrelated=None):
    """A scripted submission, optionally alongside unrelated background traffic."""
    noise = f'  fetch("{unrelated}").catch(() => {{}});' if unrelated else ''
    return page(
        '<form id="contact" onsubmit="return false;">'
        '<input id="name" name="name" type="text">'
        '<button id="send" type="button">Send</button>'
        '</form><div id="outcome"></div>'
        '<script>'
        'document.getElementById("send").addEventListener("click", async () => {'
        + noise
        + '  const body = new URLSearchParams({name: document.getElementById("name").value});'
        f'  try {{ const r = await fetch("{endpoint}", {{method: "POST", body}});'
        '    const t = await r.text();'
        '    const box = document.createElement("div");'
        '    box.className = "success-message";'
        '    box.textContent = t;'
        '    document.getElementById("outcome").replaceChildren(box);'
        '  } catch (e) {'
        '    document.getElementById("outcome").textContent = "submission failed";'
        '  }'
        '});'
        '</script>'
    )


WEBSOCKET_FORM = page(
    '<form id="contact"><input id="name" type="text">'
    '<button id="send" type="button">Send</button></form>'
    '<div id="outcome"></div>'
    '<script>'
    'document.getElementById("send").addEventListener("click", () => {'
    '  try { new WebSocket("ws://' + INTERNAL_HOST + '/live"); } catch (e) {}'
    '  document.getElementById("outcome").textContent = "Thanks for your message";'
    '});'
    '</script>'
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def respond(self, status, body, content_type='text/html', extra_headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, status, location):
        self.send_response(status)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/contact':
            self.respond(200, contact_form())
            return
        if path == '/contact-ajax':
            self.respond(200, AJAX_FORM)
            return
        if path == '/contact-private-xhr':
            self.respond(200, PRIVATE_XHR_FORM)
            return
        if path == '/contact-websocket':
            self.respond(200, WEBSOCKET_FORM)
            return
        if path == '/contact-to-internal':
            self.respond(200, contact_form(action=f'http://{INTERNAL_HOST}/submit'))
            return
        if path == '/contact-redirect-internal':
            self.respond(200, contact_form(action='/submit-redirect-internal'))
            return
        if path == '/contact-post-submit-internal':
            self.respond(200, contact_form(action='/submit-then-internal'))
            return
        if path == '/contact-303':
            self.respond(200, contact_form(action='/submit-303'))
            return
        if path == '/contact-307':
            self.respond(200, contact_form(action='/submit-307'))
            return
        if path == '/ajax-to-private':
            self.respond(200, ajax_form(f'http://{METADATA_HOST}/latest/meta-data/'))
            return
        if path == '/ajax-with-unrelated-blocked':
            self.respond(
                200,
                ajax_form('/ajax-submit', unrelated=f'http://{INTERNAL_HOST}/beacon'),
            )
            return
        if path == '/contact-with-blocked-image':
            self.respond(
                200,
                contact_form(extra=f'<img src="http://{METADATA_HOST}/pixel.png" alt="x">'),
            )
            return
        if path == '/contact-to-unrelated-public':
            self.respond(
                200,
                contact_form(action=f'http://{UNRELATED_HOST}:{TRIPWIRE_PORT[0]}/spam'),
            )
            return
        if path == '/contact-to-sibling':
            self.respond(
                200,
                contact_form(action=f'http://{SIBLING_HOST}:{TRIPWIRE_PORT[0]}/submit'),
            )
            return
        if path == '/contact-other-port':
            # Same host, different effective port: a different origin.
            self.respond(
                200,
                contact_form(action=f'http://{SITE_HOST}:{TRIPWIRE_PORT[0]}/submit'),
            )
            return
        if path == '/contact-cross-subdomain':
            self.respond(
                200,
                contact_form(action=f'http://{SITE_HOST}:{self.server.server_address[1]}/submit'),
            )
            return
        if path == '/contact-already-thanks':
            # The confirmation is on the page before anything is submitted.
            already = b'<div class="success-message">Thanks for your message</div>'
            self.respond(200, contact_form(action='/submit-silent') + already)
            return
        if path == '/contact-no-op-submit':
            self.respond(
                200,
                page(
                    '<div class="success-message">Thanks for your message</div>'
                    '<form id="contact" onsubmit="return false;">'
                    '<input id="name" type="text">'
                    '<button id="send" type="button">Send</button>'
                    '</form>'
                ),
            )
            return
        if path == '/contact-no-submit':
            self.respond(200, page('<form id="contact"><input id="name"></form>'))
            return
        if path == '/contact-csrf':
            token = uuid.uuid4().hex
            session = uuid.uuid4().hex
            TOKENS[session] = token
            self.respond(
                200,
                contact_form(
                    action='/submit-csrf',
                    hidden=f'<input type="hidden" name="csrf" value="{token}">',
                ),
                extra_headers=(('Set-Cookie', f'sessionid={session}; Path=/'),),
            )
            return
        if path == '/thanks':
            self.respond(200, page('<div class="success-message">Thanks for your message</div>'))
            return

        self.respond(404, page('not found'))

    def do_POST(self):
        path = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        SUBMISSIONS.record(
            {
                'path': path,
                'method': 'POST',
                'body': raw.decode('utf-8', 'replace'),
                'content_type': self.headers.get('Content-Type'),
                'cookie': self.headers.get('Cookie'),
            }
        )

        if path == '/submit':
            self.respond(200, page('<div class="success-message">Thanks for your message</div>'))
            return
        if path == '/ajax-submit':
            self.respond(200, b'Thanks for your message', content_type='text/plain')
            return
        if path == '/submit-redirect-internal':
            self.redirect(302, f'http://{INTERNAL_HOST}/swallowed')
            return
        if path == '/submit-then-internal':
            self.redirect(302, f'http://{METADATA_HOST}/latest/meta-data/')
            return
        if path == '/submit-silent':
            self.respond(
                200,
                contact_form() + b'<div class="success-message">Thanks for your message</div>',
            )
            return
        if path == '/submit-303':
            self.redirect(303, '/thanks')
            return
        if path == '/submit-307':
            self.redirect(307, '/submit-307-final')
            return
        if path == '/submit-307-final':
            self.respond(200, page('<div class="success-message">Thanks for your message</div>'))
            return
        if path == '/submit-csrf':
            fields = urllib.parse.parse_qs(raw.decode())
            cookie = self.headers.get('Cookie') or ''
            session = ''
            for part in cookie.split(';'):
                name, _, value = part.strip().partition('=')
                if name == 'sessionid':
                    session = value
            supplied = (fields.get('csrf') or [''])[0]
            if session and TOKENS.get(session) == supplied:
                self.respond(
                    200, page('<div class="success-message">Thanks for your message</div>')
                )
            else:
                self.respond(403, page('<div>CSRF check failed</div>'))
            return

        self.respond(404, page('not found'))

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def fixture_server():
    server = ThreadingHTTPServer((FIXTURE_IP, 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://{FIXTURE_IP}:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


@pytest.fixture(scope='module')
def forbidden_port():
    server = socketserver.ThreadingTCPServer((FIXTURE_IP, 0), CountingHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    TRIPWIRE_PORT[0] = server.server_address[1]
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def gate_harness(monkeypatch):
    """Reachable fixture, genuinely private forbidden hosts, zeroed counters."""
    real_getaddrinfo = socket.getaddrinfo
    real_ensure_public_ip = ssrf.ensure_public_ip

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in STUBBED_DNS:
            address = STUBBED_DNS[host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]
        if host in PUBLIC_STUBBED_DNS:
            # Resolves to the loopback the harness treats as public, so these
            # hosts are both allowed by SSRF and actually reachable.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (FIXTURE_IP, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    def ensure_public_ip(value):
        # The one fiction: the fixture server's own address. Every private
        # address these tests aim at goes to the real rules.
        if str(value) == FIXTURE_IP:
            return value
        return real_ensure_public_ip(value)

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(ssrf, 'ensure_public_ip', ensure_public_ip)
    TRIPWIRE.reset()
    SUBMISSIONS.reset()
    TOKENS.clear()


@pytest.fixture
def run_flow(forbidden_port):
    rules = ','.join(f'MAP {host} {FIXTURE_IP}:{forbidden_port}' for host in STUBBED_DNS)

    def run(url, plan, timeout_seconds=20):
        return run_flow_check(
            url=url,
            timeout_seconds=timeout_seconds,
            now=None,
            plan=plan,
            launch_args=(f'--host-resolver-rules={rules}',),
        )

    return run


def plan(**overrides):
    data = {
        'submit_selector': '#send',
        'fields': (
            FieldAction('#name', FlowFieldType.TEXT, 'Uptora Monitor'),
            FieldAction('#email', FlowFieldType.EMAIL, 'monitor@example.com'),
            FieldAction('#message', FlowFieldType.TEXTAREA, 'Checking the contact form.'),
        ),
        'success_text': 'Thanks for your message',
    }
    data.update(overrides)
    return ContactFormPlan(**data)


class TestRealSubmission:
    def test_a_full_navigation_form_succeeds(self, run_flow, fixture_server):
        outcome = run_flow(f'{fixture_server}/contact', plan())

        assert outcome.is_success, outcome.error_message
        assert outcome.screenshot is None
        assert SUBMISSIONS.last['path'] == '/submit'

    def test_the_typed_values_reach_the_server(self, run_flow, fixture_server):
        outcome = run_flow(f'{fixture_server}/contact', plan())

        assert outcome.is_success, outcome.error_message
        body = urllib.parse.parse_qs(SUBMISSIONS.last['body'])
        assert body['name'] == ['Uptora Monitor']
        assert body['email'] == ['monitor@example.com']
        assert body['message'] == ['Checking the contact form.']

    def test_the_method_and_content_type_are_preserved(self, run_flow, fixture_server):
        run_flow(f'{fixture_server}/contact', plan())

        assert SUBMISSIONS.last['method'] == 'POST'
        assert 'application/x-www-form-urlencoded' in SUBMISSIONS.last['content_type']

    def test_checkbox_and_select_are_applied(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact',
            plan(
                fields=(
                    FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),
                    FieldAction('#consent', FlowFieldType.CHECKBOX, 'true'),
                    FieldAction('#topic', FlowFieldType.SELECT, 'support'),
                )
            ),
        )

        assert outcome.is_success, outcome.error_message
        body = urllib.parse.parse_qs(SUBMISSIONS.last['body'])
        assert body['consent'] == ['on']
        assert body['topic'] == ['support']

    def test_an_ajax_form_succeeds_without_navigating(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact-ajax',
            plan(
                fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),),
                success_text='Thanks for your message',
                success_selector='.success-message',
            ),
        )

        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/ajax-submit'

    def test_a_success_url_assertion_sees_the_redirect_target(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact-303',
            plan(success_text=None, success_url_contains='/thanks'),
        )

        assert outcome.is_success, outcome.error_message
        assert outcome.final_url.endswith('/thanks')

    def test_a_missing_submit_control_is_reported(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact-no-submit',
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),)),
            timeout_seconds=10,
        )

        assert outcome.error_type == ErrorType.FLOW_SUBMIT_NOT_FOUND
        assert outcome.screenshot

    def test_a_missing_field_is_reported(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact',
            plan(fields=(FieldAction('#nonexistent', FlowFieldType.TEXT, 'x'),)),
            timeout_seconds=10,
        )

        assert outcome.error_type == ErrorType.FLOW_FIELD_NOT_FOUND
        assert outcome.screenshot

    def test_a_wrong_success_text_fails_with_evidence(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact',
            plan(success_text='Totally absent phrase'),
            timeout_seconds=10,
        )

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING
        assert outcome.screenshot and outcome.screenshot.startswith(b'\x89PNG')


class TestRedirectMethodSemantics:
    def test_303_switches_the_follow_up_to_get(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact-303',
            plan(success_text=None, success_url_contains='/thanks'),
        )

        assert outcome.is_success, outcome.error_message
        # Only the original POST was recorded; the follow-up was a GET.
        assert [entry['path'] for entry in SUBMISSIONS.posts] == ['/submit-303']

    def test_307_preserves_the_method_and_body(self, run_flow, fixture_server):
        outcome = run_flow(f'{fixture_server}/contact-307', plan())

        assert outcome.is_success, outcome.error_message
        paths = [entry['path'] for entry in SUBMISSIONS.posts]
        assert paths == ['/submit-307', '/submit-307-final']
        # The body survived the hop intact.
        replayed = urllib.parse.parse_qs(SUBMISSIONS.posts[-1]['body'])
        assert replayed['name'] == ['Uptora Monitor']


class TestSessionState:
    def test_a_csrf_protected_form_succeeds(self, run_flow, fixture_server):
        """The token comes from the DOM and the cookie from the page load.

        Both have to survive the fetch boundary: responses are fetched outside
        Chromium and fulfilled back in, so Set-Cookie must reach its jar and the
        Cookie header must come back out on the submission.
        """
        outcome = run_flow(
            f'{fixture_server}/contact-csrf',
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),)),
        )

        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/submit-csrf'

    def test_the_session_cookie_reaches_the_submission(self, run_flow, fixture_server):
        run_flow(
            f'{fixture_server}/contact-csrf',
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),)),
        )

        assert 'sessionid=' in (SUBMISSIONS.last['cookie'] or '')

    def test_the_hidden_token_is_submitted_unchanged(self, run_flow, fixture_server):
        run_flow(
            f'{fixture_server}/contact-csrf',
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),)),
        )

        submitted = urllib.parse.parse_qs(SUBMISSIONS.last['body'])
        session = SUBMISSIONS.last['cookie'].split('sessionid=')[1].split(';')[0]
        assert submitted['csrf'] == [TOKENS[session]]


class TestWriteSsrf:
    """A form is a write. None of these may reach the forbidden address."""

    def test_a_form_posting_to_a_private_host_never_connects(self, run_flow, fixture_server):
        outcome = run_flow(f'{fixture_server}/contact-to-internal', plan(), timeout_seconds=15)

        assert TRIPWIRE.connections == 0
        # Named as a refusal, not as a missing confirmation: the form is fine,
        # the destination is not.
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_submission_redirecting_to_a_private_host_never_connects(
        self, run_flow, fixture_server
    ):
        outcome = run_flow(
            f'{fixture_server}/contact-redirect-internal', plan(), timeout_seconds=15
        )

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_post_submit_navigation_to_a_private_host_never_connects(
        self, run_flow, fixture_server
    ):
        outcome = run_flow(
            f'{fixture_server}/contact-post-submit-internal', plan(), timeout_seconds=15
        )

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_private_xhr_during_submission_never_connects(self, run_flow, fixture_server):
        """The page's own script tries the metadata endpoint and is refused.

        The flow still succeeds: the refusal is Uptora protecting itself, and
        the form's visible outcome is what the assertion measures.
        """
        outcome = run_flow(
            f'{fixture_server}/contact-private-xhr',
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),)),
        )

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_websocket_during_submission_never_connects(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact-websocket',
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),)),
        )

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_flow_aimed_at_a_private_url_never_starts_a_browser(self, run_flow):
        outcome = run_flow('http://10.0.0.5/contact', plan(), timeout_seconds=10)

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_public_submission_still_works(self, run_flow, fixture_server):
        """The control: the gate does not simply block everything."""
        outcome = run_flow(f'{fixture_server}/contact', plan())

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last is not None


class TestSubmissionAttribution:
    """The blocked request has to be the submission, not merely contemporaneous."""

    def test_an_ajax_submission_to_a_private_host_fails_as_blocked(self, run_flow, fixture_server):
        """A. the submission itself aims at a private address."""
        outcome = run_flow(
            f'{fixture_server}/ajax-to-private',
            plan(
                fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),),
                success_text='Thanks for your message',
            ),
            timeout_seconds=15,
        )

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_form_post_to_a_private_host_fails_as_blocked(self, run_flow, fixture_server):
        """B. a plain form navigation aimed at a private address."""
        outcome = run_flow(f'{fixture_server}/contact-to-internal', plan(), timeout_seconds=15)

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_an_unrelated_blocked_image_does_not_fail_a_good_submission(
        self, run_flow, fixture_server
    ):
        """C. the page loads a private image; the form itself is fine."""
        outcome = run_flow(f'{fixture_server}/contact-with-blocked-image', plan())

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/submit'

    def test_an_unrelated_blocked_fetch_does_not_fail_a_good_ajax_submission(
        self, run_flow, fixture_server
    ):
        """D. a blocked background fetch fires alongside a valid submission."""
        outcome = run_flow(
            f'{fixture_server}/ajax-with-unrelated-blocked',
            plan(
                fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),),
                success_text='Thanks for your message',
            ),
        )

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/ajax-submit'


class TestDestinationPolicy:
    def test_a_same_origin_submission_is_allowed(self, run_flow, fixture_server):
        outcome = run_flow(f'{fixture_server}/contact', plan())

        assert outcome.is_success, outcome.error_message

    def test_a_www_to_apex_submission_is_allowed(self, run_flow, fixture_server):
        """The monitored host is www.; the form posts to the apex."""
        port = fixture_server.rsplit(':', 1)[1]
        outcome = run_flow(f'http://{WWW_SITE_HOST}:{port}/contact-cross-subdomain', plan())

        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/submit'

    def test_an_unrelated_public_destination_is_refused(self, run_flow, fixture_server):
        """The spam-relay case: a page aiming its form at somebody else's site."""
        outcome = run_flow(
            f'{fixture_server}/contact-to-unrelated-public', plan(), timeout_seconds=15
        )

        assert outcome.error_type == ErrorType.FLOW_DESTINATION_NOT_ALLOWED
        assert UNRELATED_HOST in outcome.error_message

    def test_the_unrelated_destination_is_never_contacted(self, run_flow, fixture_server):
        """Refused before the fetch, so nothing reaches the third party.

        The destination is a genuinely reachable, gate-approved public host, so
        a broken policy would connect and be counted rather than merely fail.
        """
        run_flow(f'{fixture_server}/contact-to-unrelated-public', plan(), timeout_seconds=15)

        assert TRIPWIRE.connections == 0

    def test_a_private_destination_is_still_a_blocked_target(self, run_flow, fixture_server):
        """The two policies stay distinguishable."""
        outcome = run_flow(f'{fixture_server}/contact-to-internal', plan(), timeout_seconds=15)

        assert outcome.error_type == ErrorType.BLOCKED_TARGET


class TestPreExistingSuccessState:
    def test_a_confirmation_already_on_the_page_does_not_prove_submission(
        self, run_flow, fixture_server
    ):
        """The button does nothing; the thank-you was always there."""
        outcome = run_flow(
            f'{fixture_server}/contact-no-op-submit',
            plan(
                fields=(FieldAction('#name', FlowFieldType.TEXT, 'Uptora'),),
                success_text='Thanks for your message',
                success_selector='.success-message',
            ),
            timeout_seconds=10,
        )

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.FLOW_SUBMIT_ERROR
        assert SUBMISSIONS.last is None

    def test_a_real_submission_still_succeeds_when_the_text_pre_exists(
        self, run_flow, fixture_server
    ):
        """The confirmation was already visible, but a POST genuinely happened."""
        outcome = run_flow(
            f'{fixture_server}/contact-already-thanks',
            plan(success_text='Thanks for your message'),
        )

        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/submit-silent'


class TestSameOriginPolicy:
    """The MVP boundary: same origin, plus www <-> apex.

    Each refusal points at a genuinely reachable, gate-approved destination on
    the tripwire, so a broken policy would connect and be counted rather than
    merely fail.
    """

    def test_the_exact_origin_is_allowed(self, run_flow, fixture_server):
        outcome = run_flow(f'{fixture_server}/contact', plan())

        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/submit'
        assert TRIPWIRE.connections == 0

    def test_www_may_submit_to_the_apex(self, run_flow, fixture_server):
        port = fixture_server.rsplit(':', 1)[1]

        outcome = run_flow(f'http://{WWW_SITE_HOST}:{port}/contact-cross-subdomain', plan())

        assert outcome.is_success, outcome.error_message
        assert SUBMISSIONS.last['path'] == '/submit'

    def test_a_sibling_subdomain_is_refused_and_never_contacted(self, run_flow, fixture_server):
        port = fixture_server.rsplit(':', 1)[1]

        outcome = run_flow(
            f'http://{SITE_HOST}:{port}/contact-to-sibling', plan(), timeout_seconds=15
        )

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.FLOW_DESTINATION_NOT_ALLOWED
        assert SIBLING_HOST in outcome.error_message

    def test_a_different_port_is_refused_and_never_contacted(self, run_flow, fixture_server):
        """Same scheme and host, different effective port."""
        port = fixture_server.rsplit(':', 1)[1]

        outcome = run_flow(
            f'http://{SITE_HOST}:{port}/contact-other-port', plan(), timeout_seconds=15
        )

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.FLOW_DESTINATION_NOT_ALLOWED

    def test_an_unrelated_domain_is_refused_and_never_contacted(self, run_flow, fixture_server):
        outcome = run_flow(
            f'{fixture_server}/contact-to-unrelated-public', plan(), timeout_seconds=15
        )

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.FLOW_DESTINATION_NOT_ALLOWED

    def test_a_private_destination_is_still_reported_as_blocked(self, run_flow, fixture_server):
        """Address safety is classified before the origin rule."""
        outcome = run_flow(f'{fixture_server}/contact-to-internal', plan(), timeout_seconds=15)

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET
