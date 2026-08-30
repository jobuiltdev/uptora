"""Integration tests that drive a real headless Chromium.

Skipped automatically when the Chromium build is missing, so CI without
`playwright install chromium` still runs green on the mocked suite.

Harness design
--------------
Two local servers:

  fixture   - serves the pages under test on 127.0.0.1.
  forbidden - a raw TCP server that counts every accepted connection and is
              never supposed to receive one.

Two accommodations, both in the test process, neither touching production code:

  * ensure_public_ip is wrapped to accept exactly 127.0.0.1, so the fixture
    server is reachable. Every other address -- including all the private ones
    these tests aim at -- is still adjudicated by the real function.
  * DNS is stubbed so internal.fixture.test resolves to 10.0.0.5 and
    metadata.fixture.test to 169.254.169.254, giving the gate genuine private
    addresses to refuse.

Chromium is launched with --host-resolver-rules mapping those same two
hostnames to the forbidden server. That is the tripwire: if Chromium ever
resolves and connects on its own, it lands on the counter and the test fails.
A blocked target therefore has to prove connections == 0, not merely that
Uptora reported a block.
"""

import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from monitors import ssrf
from monitors.checks.browser import run_browser_check
from monitors.models import ErrorType

FIXTURE_IP = '127.0.0.1'

INTERNAL_HOST = 'internal.fixture.test'
METADATA_HOST = 'metadata.fixture.test'
STUBBED_DNS = {INTERNAL_HOST: '10.0.0.5', METADATA_HOST: '169.254.169.254'}

PNG = bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010806000000')


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
    """Counts connections to an address that must never be contacted."""

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
    """Counts the TCP connection itself, before any HTTP is spoken.

    A speculative preconnect that opens a socket and sends nothing still counts,
    which is the point: the question is whether the address was contacted at all.
    """

    def handle(self):
        TRIPWIRE.record()
        try:
            self.rfile.readline()
        except OSError:
            return
        body = b'this response must never be seen'
        self.wfile.write(
            b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n%s'
            % (len(body), body)
        )


def html(body):
    page = (
        '<!doctype html><html><body><h1>Uptora is up</h1>'
        f'<div id="ready">ok</div>{body}</body></html>'
    )
    return page.encode()


REDIRECTS = {
    '/redirect-ok': '/ok',
    '/redirect-to-internal': f'http://{INTERNAL_HOST}/secret',
    '/redirect-to-metadata': f'http://{METADATA_HOST}/latest/meta-data/',
    '/redirect-to-private-literal': 'http://10.0.0.5/secret',
    '/loop': '/loop',
    # Multi-hop: public -> public -> private.
    '/hop-one': '/hop-two',
    '/hop-two': f'http://{INTERNAL_HOST}/secret',
    # Multi-hop entirely within public space.
    '/public-hop-one': '/public-hop-two',
    '/public-hop-two': '/logo.png',
    '/img-redirect-internal': f'http://{INTERNAL_HOST}/pixel.png',
    '/script-redirect-metadata': f'http://{METADATA_HOST}/latest/meta-data/',
    '/frame-redirect-internal': '/frame-hop',
    '/frame-hop': f'http://{INTERNAL_HOST}/frame.html',
}

PAGES = {
    '/ok': (200, 'text/html', html('')),
    '/noisy': (
        200,
        'text/html',
        html("<script>console.error('noise'); throw new Error('boom');</script>"),
    ),
    # A. public page -> public image URL -> 302 to a private address.
    '/image-redirect': (200, 'text/html', html('<img src="/img-redirect-internal" alt="x">')),
    # B. public script URL -> 302 to the metadata endpoint.
    '/script-redirect': (
        200,
        'text/html',
        html('<script src="/script-redirect-metadata"></script>'),
    ),
    # C. public iframe -> public redirect -> private address.
    '/iframe-redirect': (
        200,
        'text/html',
        html('<iframe src="/frame-redirect-internal"></iframe>'),
    ),
    # D. public resource -> public redirect -> public final target.
    '/public-redirect-chain': (200, 'text/html', html('<img id="pic" src="/public-hop-one">')),
    '/direct-private-subresource': (
        200,
        'text/html',
        html(f'<img src="http://{METADATA_HOST}/latest/" alt="x">'),
    ),
    '/public-subresource': (200, 'text/html', html('<img src="/logo.png" alt="x">')),
    '/websocket-private': (
        200,
        'text/html',
        html(f"<script>new WebSocket('ws://{INTERNAL_HOST}/live');</script>"),
    ),
    '/websocket-loopback': (
        200,
        'text/html',
        html("<script>new WebSocket('ws://127.0.0.1:9001/live');</script>"),
    ),
    '/file-scheme': (200, 'text/html', html('<img src="file:///C:/Windows/win.ini" alt="x">')),
    '/preconnect-private': (
        200,
        'text/html',
        html(f'<link rel="preconnect" href="http://{INTERNAL_HOST}">'),
    ),
    '/logo.png': (200, 'image/png', PNG),
    '/app.js': (200, 'application/javascript', b'window.loaded = true;'),
    '/boom': (500, 'text/html', b'<html><body>server error</body></html>'),
    '/gone': (404, 'text/html', b'<html><body>not here</body></html>'),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]

        if path in REDIRECTS:
            self.send_response(302)
            self.send_header('Location', REDIRECTS[path])
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        status, content_type, body = PAGES.get(path, (404, 'text/html', b'missing'))
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def gate_harness(monkeypatch):
    """Reachable fixture, genuinely private forbidden hosts, zeroed tripwire."""
    real_getaddrinfo = socket.getaddrinfo
    real_ensure_public_ip = ssrf.ensure_public_ip

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in STUBBED_DNS:
            address = STUBBED_DNS[host]
            family = socket.AF_INET6 if ':' in address else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 6, '', (address, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    def ensure_public_ip(value):
        # The one fiction in this harness: the fixture server's own address.
        # Everything else goes to the real rules, unchanged.
        if str(value) == FIXTURE_IP:
            return value
        return real_ensure_public_ip(value)

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(ssrf, 'ensure_public_ip', ensure_public_ip)
    TRIPWIRE.reset()


@pytest.fixture
def check(forbidden_port):
    """run_browser_check with Chromium's resolver wired to the tripwire."""
    rules = ','.join(f'MAP {host} {FIXTURE_IP}:{forbidden_port}' for host in STUBBED_DNS)

    def run(url, **kwargs):
        kwargs.setdefault('timeout_seconds', 15)
        kwargs.setdefault('now', None)
        kwargs.setdefault('launch_args', (f'--host-resolver-rules={rules}',))
        return run_browser_check(url=url, **kwargs)

    return run


class TestHarness:
    """The harness itself has to work, or every assertion below is vacuous."""

    def test_the_forbidden_server_counts_a_real_connection(self, forbidden_port):
        with socket.create_connection((FIXTURE_IP, forbidden_port), timeout=5) as sock:
            sock.sendall(b'GET / HTTP/1.0\r\n\r\n')
            sock.recv(64)

        assert TRIPWIRE.connections == 1

    def test_the_gate_still_refuses_a_private_hostname(self, check):
        outcome = check(f'http://{INTERNAL_HOST}/secret')

        assert outcome.error_type == ErrorType.BLOCKED_TARGET
        assert TRIPWIRE.connections == 0


class TestRealRendering:
    def test_a_real_page_loads_and_succeeds(self, check, fixture_server):
        outcome = check(f'{fixture_server}/ok')

        assert outcome.is_success, outcome.error_message
        assert outcome.status_code == 200
        assert outcome.screenshot is None

    def test_expected_text_is_found(self, check, fixture_server):
        outcome = check(f'{fixture_server}/ok', expected_text='Uptora is up')

        assert outcome.is_success, outcome.error_message

    def test_missing_expected_text_fails_with_a_screenshot(self, check, fixture_server):
        outcome = check(f'{fixture_server}/ok', expected_text='Totally absent phrase')

        assert outcome.error_type == ErrorType.EXPECTED_TEXT_MISSING
        assert outcome.screenshot and outcome.screenshot.startswith(b'\x89PNG')

    def test_expected_selector_is_found(self, check, fixture_server):
        outcome = check(f'{fixture_server}/ok', expected_selector='#ready')

        assert outcome.is_success, outcome.error_message

    def test_missing_expected_selector_fails(self, check, fixture_server):
        outcome = check(f'{fixture_server}/ok', expected_selector='#nonexistent')

        assert outcome.error_type == ErrorType.EXPECTED_SELECTOR_MISSING

    def test_both_expectations_together(self, check, fixture_server):
        outcome = check(
            f'{fixture_server}/ok', expected_text='Uptora is up', expected_selector='#ready'
        )

        assert outcome.is_success, outcome.error_message

    def test_a_500_page_fails(self, check, fixture_server):
        outcome = check(f'{fixture_server}/boom')

        assert outcome.error_type == ErrorType.HTTP_ERROR
        assert outcome.status_code == 500

    def test_a_404_page_fails(self, check, fixture_server):
        outcome = check(f'{fixture_server}/gone')

        assert outcome.status_code == 404
        assert not outcome.is_success

    def test_a_public_redirect_is_followed(self, check, fixture_server):
        outcome = check(f'{fixture_server}/redirect-ok', expected_text='Uptora is up')

        assert outcome.is_success, outcome.error_message
        assert outcome.final_url.endswith('/ok')

    def test_a_redirect_loop_is_cut_off(self, check, fixture_server):
        outcome = check(f'{fixture_server}/loop')

        assert outcome.error_type == ErrorType.TOO_MANY_REDIRECTS

    def test_console_and_page_errors_do_not_fail_a_rendered_page(self, check, fixture_server):
        outcome = check(f'{fixture_server}/noisy', expected_text='Uptora is up')

        assert outcome.is_success, outcome.error_message


class TestSubresourceRedirectSsrf:
    """The gap this pass exists to close.

    Every case asserts the forbidden endpoint was never contacted, not merely
    that Uptora reported a block.
    """

    def test_a_image_redirecting_to_a_private_address_never_connects(self, check, fixture_server):
        """A. public page -> public image URL -> 302 to a private address."""
        outcome = check(f'{fixture_server}/image-redirect', expected_text='Uptora is up')

        assert TRIPWIRE.connections == 0
        # The page itself is fine; we simply refused to fetch the image.
        assert outcome.is_success, outcome.error_message

    def test_a_script_redirecting_to_the_metadata_endpoint_never_connects(
        self, check, fixture_server
    ):
        """B. public script URL -> 302 to 169.254.169.254."""
        outcome = check(f'{fixture_server}/script-redirect', expected_text='Uptora is up')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_an_iframe_redirecting_to_a_private_address_never_connects(self, check, fixture_server):
        """C. public iframe -> public redirect -> private address."""
        outcome = check(f'{fixture_server}/iframe-redirect', expected_text='Uptora is up')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_fully_public_redirect_chain_still_loads(self, check, fixture_server):
        """D. public resource -> public redirect -> public final target."""
        outcome = check(f'{fixture_server}/public-redirect-chain', expected_selector='#pic')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_multi_hop_chain_is_stopped_before_the_private_hop(self, check, fixture_server):
        """E. navigation: public -> public -> private."""
        outcome = check(f'{fixture_server}/hop-one')

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_navigation_redirecting_to_a_private_host_never_connects(self, check, fixture_server):
        outcome = check(f'{fixture_server}/redirect-to-internal')

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_navigation_redirecting_to_the_metadata_endpoint_never_connects(
        self, check, fixture_server
    ):
        outcome = check(f'{fixture_server}/redirect-to-metadata')

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_navigation_redirecting_to_a_private_literal_is_blocked(self, check, fixture_server):
        outcome = check(f'{fixture_server}/redirect-to-private-literal')

        assert TRIPWIRE.connections == 0
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_directly_named_private_subresource_never_connects(self, check, fixture_server):
        outcome = check(f'{fixture_server}/direct-private-subresource', expected_selector='#ready')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_public_subresources_still_load(self, check, fixture_server):
        outcome = check(f'{fixture_server}/public-subresource', expected_selector='#ready')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_speculative_preconnect_never_reaches_the_address(self, check, fixture_server):
        """Preconnect hints open sockets outside the request pipeline."""
        outcome = check(f'{fixture_server}/preconnect-private', expected_selector='#ready')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message


class TestSchemePolicy:
    def test_a_websocket_to_a_private_host_never_connects(self, check, fixture_server):
        outcome = check(f'{fixture_server}/websocket-private', expected_selector='#ready')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_websocket_to_loopback_never_connects(self, check, fixture_server):
        outcome = check(f'{fixture_server}/websocket-loopback', expected_selector='#ready')

        assert TRIPWIRE.connections == 0
        assert outcome.is_success, outcome.error_message

    def test_a_file_subresource_does_not_break_the_page(self, check, fixture_server):
        outcome = check(f'{fixture_server}/file-scheme', expected_selector='#ready')

        assert outcome.is_success, outcome.error_message

    @pytest.mark.parametrize(
        'url',
        [
            'file:///C:/Windows/win.ini',
            'ftp://example.com/x',
            'ws://example.com/live',
            'wss://example.com/live',
        ],
    )
    def test_non_web_schemes_are_refused_as_a_target(self, check, url):
        outcome = check(url, timeout_seconds=10)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BLOCKED_TARGET
        assert TRIPWIRE.connections == 0


class TestDirectPrivateTargets:
    @pytest.mark.parametrize(
        'url',
        [
            'http://10.0.0.5/internal',
            'http://169.254.169.254/latest/meta-data/',
            'http://[::1]:8080/',
            'http://localhost:8080/',
            'http://192.168.1.1/',
        ],
    )
    def test_a_private_target_is_refused(self, check, url):
        outcome = check(url, timeout_seconds=10)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BLOCKED_TARGET
        assert TRIPWIRE.connections == 0


class TestRealResourceSafety:
    """Chromium must be released on every path out, or workers accumulate zombies."""

    def test_a_clean_run_releases_everything(self, fixture_server):
        from monitors.checks.playwright_session import PlaywrightSession

        session = PlaywrightSession(timeout_ms=15000)
        with session:
            assert session._page is not None
            session.navigate(f'{fixture_server}/ok', 15000)

        assert session._page is None
        assert session._context is None
        assert session._browser is None
        assert session._playwright is None
        assert session._client is None

    def test_an_exception_inside_the_block_still_tears_down(self, fixture_server):
        from monitors.checks.playwright_session import PlaywrightSession

        session = PlaywrightSession(timeout_ms=15000)
        with pytest.raises(RuntimeError), session:
            session.navigate(f'{fixture_server}/ok', 15000)
            raise RuntimeError('something went wrong mid-check')

        assert session._browser is None
        assert session._playwright is None
        assert session._client is None

    def test_repeated_checks_stay_healthy(self, check, fixture_server):
        """Each check gets its own browser, so a failing one cannot poison the next."""
        outcomes = [check(f'{fixture_server}{path}') for path in ('/ok', '/boom', '/loop', '/ok')]

        assert [o.is_success for o in outcomes] == [True, False, False, True]
