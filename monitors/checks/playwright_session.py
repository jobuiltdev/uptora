"""The Chromium driver.

The only module that imports Playwright. It owns the browser lifecycle for one
check and translates Playwright's exceptions into the small vocabulary declared
in browser.py, so the decision logic never has to know what a
playwright._impl._errors.Error is.

Network architecture
--------------------
Chromium never opens an HTTP or HTTPS connection of its own. Every such request
is intercepted, fetched by Uptora through monitors.checks.fetching -- the same
validated, IP-pinned path the HTTP monitor uses -- and handed back with
route.fulfill. Chromium receives a response it never went out for.

That is what closes two holes that interception alone leaves open:

  - Chromium follows redirects inside its own network stack and does not
    re-enter the route handler for the new hop, so a public image URL could
    302 straight to 169.254.169.254 unseen. Now no hop exists that we did not
    fetch ourselves, and every one is validated before it is contacted.
  - Chromium resolves hostnames with its own resolver, so validating in Python
    and then handing Chromium a hostname left a time-of-check/time-of-use gap.
    Now the validated IP is the one connected to, because the connection is
    ours.

Certificate verification is untouched: fetching pins the IP but still presents
the real hostname for SNI and validates the chain against it.

Every check gets its own Playwright process, browser, context and page. A shared
browser would be faster, but a crashed or poisoned browser would then leak into
unrelated customers' checks, and there is no worker pool yet to own its
lifetime. Correctness first; pooling belongs with the scheduler.
"""

from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from monitors.checks.browser import (
    INERT_SCHEMES,
    BrowserFailure,
    BrowserTimeout,
    Diagnostics,
    Navigation,
    NavigationFailed,
)
from monitors.checks.fetching import (
    TooManyRedirectsError,
    fetch_validated,
    forwardable_headers,
)
from monitors.ssrf import ALLOWED_SCHEMES, BlockedTargetError, TargetResolutionError

# A full load rather than domcontentloaded: the question a browser monitor
# answers is whether the page renders, which includes its subresources.
WAIT_UNTIL = 'load'

# Surfaces as net::ERR_BLOCKED_BY_CLIENT, which the classifier recognises.
ABORT_CODE = 'blockedbyclient'
FETCH_FAILED_CODE = 'failed'

# Describe a connection we are not passing on, or are re-deriving.
STRIPPED_RESPONSE_HEADERS = frozenset(
    {
        'content-encoding',
        'content-length',
        'transfer-encoding',
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'te',
        'trailer',
        'upgrade',
    }
)

# Chromium can open sockets outside the request pipeline -- speculative
# preconnects, DNS prefetch, hyperlink auditing, component updates. None of
# those pass through route interception, so they are switched off rather than
# left as an unpoliced way to touch an address.
HARDENING_ARGS = (
    '--disable-background-networking',
    '--dns-prefetch-disable',
    '--no-pings',
    '--disable-features=NetworkPrediction,PreconnectToSearch,OptimizationHints,Translate',
    '--disable-client-side-phishing-detection',
    '--disable-sync',
    '--no-first-run',
    '--no-default-browser-check',
)


class PlaywrightSession:
    """Owns Chromium for the duration of one check.

    Used as a context manager so the browser is torn down on every path out,
    including navigation failures, failed expectations and unexpected errors.
    """

    def __init__(self, timeout_ms, launch_args=()):
        self.timeout_ms = timeout_ms
        self.launch_args = [*HARDENING_ARGS, *launch_args]
        self.diagnostics = Diagnostics()
        # Where the main frame's redirect chain actually ended. Tracked here
        # because the page only ever sees the URL we started with: the hops
        # happen out of band.
        self.final_url = None
        # A network failure from our own fetch, which carries a far more useful
        # message than the generic abort Chromium would report.
        self.navigation_error = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._client = None

    def __enter__(self):
        try:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_ms / 1000),
                follow_redirects=False,
            )
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=self.launch_args,
                timeout=self.timeout_ms,
            )
            self._context = self._browser.new_context(
                # Service workers can issue requests outside the interception
                # path, which would be a hole in the gate.
                service_workers='block',
            )
            self._context.set_default_timeout(self.timeout_ms)
            self._context.route('**/*', self._gate)
            self._context.route_web_socket('**/*', self._gate_websocket)
            self._page = self._context.new_page()
            self._page.on('console', self._on_console)
            self._page.on('pageerror', self._on_page_error)
        except Exception as exc:
            self.__exit__(None, None, None)
            raise BrowserFailure(f'Could not start the browser: {exc}') from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        # Closed innermost first, each independently: one failing teardown must
        # not strand the rest and leave a Chromium process behind.
        for closer in (self._page, self._context, self._browser, self._client):
            if closer is not None:
                try:
                    closer.close()
                except Exception:  # noqa: BLE001 - teardown is best effort
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        self._page = self._context = self._browser = None
        self._playwright = self._client = None
        return False

    # Request gate -------------------------------------------------------

    def _gate(self, route):
        """Every request Chromium makes passes through here.

        Web content can only reach the network by one of three verdicts:
        fulfilled from a fetch we made ourselves, allowed through because it
        touches no network at all, or aborted.
        """
        url = route.request.url
        scheme = urlsplit(url).scheme.lower()

        if scheme in INERT_SCHEMES:
            # data:, blob: and about: never leave the process.
            self._safely(route.continue_)
            return

        if scheme not in ALLOWED_SCHEMES:
            # file:, ftp: and anything else that could reach a local resource.
            self.diagnostics.record_blocked(
                url, f'Scheme {scheme!r} is not permitted during a check', navigation=False
            )
            self._safely(lambda: route.abort(ABORT_CODE))
            return

        self._serve(route)

    def _serve(self, route):
        """Fetch a request ourselves and hand Chromium the answer.

        Everything the origin might have redirected to has already been resolved
        and validated by fetch_validated before any of it was contacted.
        """
        request = route.request
        try:
            result = fetch_validated(
                self._client,
                url=request.url,
                method=request.method,
                headers=forwardable_headers(request.headers),
                content=request.post_data_buffer,
            )
        except BlockedTargetError as exc:
            self.diagnostics.record_blocked(
                request.url, str(exc), navigation=self._is_main_frame_navigation(request)
            )
            self._safely(lambda: route.abort(ABORT_CODE))
            return
        except TargetResolutionError as exc:
            self.navigation_error = str(exc)
            self._safely(lambda: route.abort('namenotresolved'))
            return
        except TooManyRedirectsError:
            self.diagnostics.redirect_overflow = True
            self._safely(lambda: route.abort(ABORT_CODE))
            return
        except httpx.HTTPError as exc:
            self.navigation_error = f'{type(exc).__name__}: {exc}'
            self._safely(lambda: route.abort(FETCH_FAILED_CODE))
            return

        if self._is_main_frame_navigation(request):
            self.final_url = result.final_url

        response = result.response
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in STRIPPED_RESPONSE_HEADERS
        }
        self._safely(
            lambda: route.fulfill(
                status=response.status_code,
                headers=headers,
                body=response.content,
            )
        )

    def _gate_websocket(self, ws):
        """Refuse every WebSocket.

        ws:// and wss:// are network-capable and Chromium opens them directly,
        outside the fetch path that validates and pins everything else. Without
        this handler a page really can reach an internal address: a page loading
        ws://internal-host/ connects, which is measured in the integration
        suite.

        Registering the route and returning without calling connect_to_server
        leaves the socket in mock mode: Playwright answers the handshake and no
        packet reaches the destination. Deliberately no ws.close() here -- that
        call deadlocks when made from inside a sync route handler, and doing
        nothing already achieves the goal.

        Blocking all of them, not just private ones, is the conservative choice:
        a page proves it rendered without needing a live socket, and proxying one
        through the validated path is work this milestone does not need.
        """
        self.diagnostics.record_blocked(
            ws.url, 'WebSocket connections are not permitted during a check', navigation=False
        )

    @staticmethod
    def _is_main_frame_navigation(request):
        """True only for the top-level document, not for an iframe's."""
        try:
            return request.is_navigation_request() and request.frame.parent_frame is None
        except PlaywrightError:
            return False

    @staticmethod
    def _safely(action):
        """Routes die when the page moves on; that is not an error worth raising."""
        try:
            action()
        except PlaywrightError:
            pass

    # Page observation ---------------------------------------------------

    def _on_console(self, message):
        if message.type == 'error':
            self.diagnostics.record_console_error(message.text)

    def _on_page_error(self, error):
        self.diagnostics.record_page_error(error)

    def navigate(self, url, timeout_ms):
        try:
            response = self._page.goto(url, wait_until=WAIT_UNTIL, timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise BrowserTimeout(str(exc)) from exc
        except PlaywrightError as exc:
            # Prefer our own message: Chromium only knows the request was aborted.
            raise NavigationFailed(self.navigation_error or str(exc)) from exc

        return Navigation(
            status=response.status if response is not None else None,
            # The page believes it is still at the URL we asked for, because the
            # redirect chain was followed out of band.
            final_url=self.final_url or self._page.url,
        )

    def has_text(self, text):
        """Substring match against the page's rendered text.

        Rendered text rather than raw HTML, so a match inside a tag attribute or
        a script body does not count as the page saying something.
        """
        try:
            return text in self._page.locator('body').inner_text()
        except PlaywrightError:
            return False

    def wait_for_selector(self, selector, timeout_ms):
        """Whether the selector is present in the DOM within the remaining budget.

        'attached' rather than 'visible': the configured contract is that the
        element exists. Visibility would make layout and CSS part of the check,
        which is visual regression territory and out of scope here.
        """
        try:
            self._page.wait_for_selector(selector, timeout=max(timeout_ms, 1), state='attached')
        except (PlaywrightTimeoutError, PlaywrightError):
            return False
        return True

    def screenshot(self):
        """PNG bytes for the current page, or None if one cannot be taken."""
        try:
            return self._page.screenshot(type='png', full_page=False)
        except Exception:  # noqa: BLE001 - evidence must never displace the finding
            return None
