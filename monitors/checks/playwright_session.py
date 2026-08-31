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
    ElementNotFound,
    InteractionFailed,
    Navigation,
    NavigationFailed,
    request_allowed,
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

# Methods that change state on the far side. A submission is one of these, or a
# main-frame navigation; anything else during the submission window is the page
# getting on with its own business.
STATE_CHANGING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

# Resource types a scripted submission arrives as.
SUBMISSION_RESOURCE_TYPES = frozenset({'fetch', 'xhr'})

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


# Errors Chromium raises when a click navigates away underneath it. The click
# itself succeeded; only the follow-up bookkeeping failed.
NAVIGATION_TEARDOWN_MARKERS = (
    'execution context was destroyed',
    'target closed',
    'frame was detached',
    'navigating and changing the content',
)


def is_navigation_teardown(exc):
    text = str(exc).lower()
    return any(marker in text for marker in NAVIGATION_TEARDOWN_MARKERS)


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

        # Submission tracking. Closed until a flow opens it at click time, so a
        # browser check is entirely unaffected by any of this.
        self._submission_open = False
        self._submission_policy = None
        self.submission_events = []

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

    # Submission tracking ------------------------------------------------

    def begin_submission(self, policy=None):
        """Open the submission window, just before the submit control is clicked.

        `policy` is consulted for requests attributable to the submission and
        for nothing else, so ordinary subresources keep loading normally.
        """
        self._submission_open = True
        self._submission_policy = policy
        self.submission_events = []

    def is_submission_request(self, request):
        """Whether this request is plausibly the form being submitted.

        Two signals, both structural rather than timing-based:

          * a main-frame navigation, which is what a plain <form> does;
          * a state-changing method from fetch or xhr, which is what a scripted
            submission does.

        Deliberately narrow. Images, stylesheets, scripts and GET beacons fired
        during the same window are the page getting on with its own business,
        and blocking one of those must never fail an otherwise valid flow.
        """
        if not self._submission_open:
            return False
        if self._is_main_frame_navigation(request):
            return True
        try:
            method = (request.method or '').upper()
            resource_type = request.resource_type
        except PlaywrightError:
            return False
        return method in STATE_CHANGING_METHODS and resource_type in SUBMISSION_RESOURCE_TYPES

    def record_submission(self, url, outcome, reason=None):
        self.submission_events.append({'url': url, 'outcome': outcome, 'reason': reason})

    def submission_observed(self):
        """True once the click has produced a request we can attribute to it."""
        return bool(self.submission_events)

    def submission_refused(self, outcome):
        for event in self.submission_events:
            if event['outcome'] == outcome:
                return event
        return None

    # Request gate -------------------------------------------------------

    def _gate(self, route):
        """Every request Chromium makes passes through here.

        Web content can only reach the network by one of three verdicts:
        fulfilled from a fetch we made ourselves, allowed through because it
        touches no network at all, or aborted.
        """
        request = route.request
        url = request.url
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

        submission = self.is_submission_request(request)
        if submission and not self._submission_allowed(route, url):
            return

        self._serve(route, submission=submission)

    def _submission_allowed(self, route, url):
        """Adjudicate a submission before anything is sent.

        Address safety is settled first. A submission aimed at a private address
        is a security finding and must be reported as one, even though such a
        destination also fails the same-site rule; answering with the weaker
        reason would bury it. Both checks are resolution-only, so a refusal
        never touches the destination.
        """
        allowed, reason = request_allowed(url)
        if not allowed:
            self.record_submission(url, 'blocked', reason)
            self.diagnostics.record_blocked(url, reason, navigation=False)
            self._safely(lambda: route.abort(ABORT_CODE))
            return False

        if self._submission_policy is not None:
            permitted, reason = self._submission_policy(url)
            if not permitted:
                self.record_submission(url, 'refused', reason)
                self.diagnostics.record_blocked(url, reason, navigation=False)
                self._safely(lambda: route.abort(ABORT_CODE))
                return False

        return True

    def _serve(self, route, submission=False):
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
            if submission:
                self.record_submission(request.url, 'blocked', str(exc))
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

        if submission:
            self.record_submission(result.final_url, 'sent')
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

    # Flow interaction ---------------------------------------------------
    #
    # Each of these locates one control and performs one declarative action.
    # There is no entry point that evaluates page script: a flow can say what to
    # type where, never what to run.

    def _locate(self, selector, timeout_ms):
        """Wait for a selector to exist, or say which one did not."""
        try:
            self._page.wait_for_selector(selector, timeout=max(timeout_ms, 1), state='attached')
        except PlaywrightTimeoutError as exc:
            raise ElementNotFound(selector) from exc
        except PlaywrightError as exc:
            raise InteractionFailed(f'{selector}: {exc}') from exc
        return self._page.locator(selector).first

    def fill(self, selector, value, timeout_ms):
        """Type a value into a text, email or textarea control."""
        locator = self._locate(selector, timeout_ms)
        try:
            locator.fill(value, timeout=max(timeout_ms, 1))
        except PlaywrightError as exc:
            # The value is never quoted here: it may be sensitive, and the
            # selector is what an operator needs to fix the configuration.
            raise InteractionFailed(f'{selector}: {type(exc).__name__}') from exc

    def set_checkbox(self, selector, checked, timeout_ms):
        locator = self._locate(selector, timeout_ms)
        try:
            if checked:
                locator.check(timeout=max(timeout_ms, 1))
            else:
                locator.uncheck(timeout=max(timeout_ms, 1))
        except PlaywrightError as exc:
            raise InteractionFailed(f'{selector}: {type(exc).__name__}') from exc

    def select_option(self, selector, value, timeout_ms):
        locator = self._locate(selector, timeout_ms)
        try:
            locator.select_option(value, timeout=max(timeout_ms, 1))
        except PlaywrightError as exc:
            raise InteractionFailed(f'{selector}: {type(exc).__name__}') from exc

    def click(self, selector, timeout_ms):
        """Click a control, tolerating the navigation it may start.

        A submit button often tears down the execution context as it is
        clicked. Playwright reports that as an error even though the click
        landed, so a navigation-shaped failure is treated as success here and
        the success assertions decide what really happened.
        """
        locator = self._locate(selector, timeout_ms)
        try:
            locator.click(timeout=max(timeout_ms, 1))
        except PlaywrightTimeoutError as exc:
            raise InteractionFailed(f'{selector}: click timed out') from exc
        except PlaywrightError as exc:
            if is_navigation_teardown(exc):
                return
            raise InteractionFailed(f'{selector}: {type(exc).__name__}') from exc

    def current_url(self):
        """Where the page really is, following the chain we walked out of band.

        page.url would report the URL Chromium asked for, not the one its
        redirects ended at, because those hops are followed by the gate.
        """
        if self.final_url:
            return self.final_url
        try:
            return self._page.url
        except PlaywrightError:
            return None

    def sleep(self, milliseconds):
        """Yield to the page between polls, using Playwright's own clock."""
        try:
            self._page.wait_for_timeout(milliseconds)
        except PlaywrightError:
            pass
