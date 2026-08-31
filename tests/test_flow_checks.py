"""Contact-form flow logic, driven against a fake session.

No Chromium. contact_form.py holds no Playwright import and no database access,
so the whole flow can be walked in milliseconds. The real driver is covered by
tests/test_flow_integration.py.
"""

import socket
import time

import pytest

from monitors import ssrf
from monitors.checks.browser import (
    BrowserTimeout,
    Diagnostics,
    ElementNotFound,
    InteractionFailed,
    Navigation,
    NavigationFailed,
)
from monitors.checks.flow import run_flow_check
from monitors.checks.flows.contact_form import ContactFormPlan, FieldAction
from monitors.models import ErrorType, FlowFieldType

URL = 'https://example.com/contact'
PUBLIC_IP = '93.184.216.34'
SCREENSHOT = b'\x89PNG evidence'

SENSITIVE_VALUE = 'Priya Raman priya@example.com'


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = host if ':' in host or host[0].isdigit() else PUBLIC_IP
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (address, port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)


class FakeSession:
    """A page that can be scripted to behave like a form."""

    def __init__(
        self,
        status=200,
        final_url=URL,
        text='Contact us',
        selectors=('#name', '#email', '#message', 'button[type=submit]'),
        navigate_error=None,
        fill_error=None,
        click_error=None,
        on_submit=None,
        screenshot=SCREENSHOT,
        submits_network=True,
        submission_url=None,
        submission_blocked=False,
    ):
        self.status = status
        self._final_url = final_url
        self.text = text
        self.selectors = list(selectors)
        self.navigate_error = navigate_error
        self.fill_error = fill_error
        self.click_error = click_error
        # Called when submit is clicked, to mutate the page like a real form.
        self.on_submit = on_submit
        self._screenshot = screenshot
        # Whether clicking submit issues a request the gate would see, and what
        # the gate would decide about it.
        self.submits_network = submits_network
        self.submission_url = submission_url or URL
        self.submission_blocked = submission_blocked
        self.submission_policy = None
        self.submission_events = []

        self.diagnostics = Diagnostics()
        self.filled = []
        self.checked = []
        self.selected = []
        self.clicked = []
        self.sleeps = 0
        self.closed = False
        self.screenshots_taken = 0

    # lifecycle
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def factory(self):
        def make(timeout_ms, launch_args=()):
            self.timeout_ms = timeout_ms
            return self

        return make

    # page
    def navigate(self, url, timeout_ms):
        if self.navigate_error is not None:
            raise self.navigate_error
        return Navigation(status=self.status, final_url=self._final_url)

    def has_text(self, text):
        return text in self.text

    def wait_for_selector(self, selector, timeout_ms):
        return selector in self.selectors

    def current_url(self):
        return self._final_url

    def screenshot(self):
        self.screenshots_taken += 1
        return self._screenshot

    def sleep(self, milliseconds):
        # Yield for real, so a polling assertion idles instead of spinning the
        # CPU for its whole budget the way a no-op stub would.
        self.sleeps += 1
        time.sleep(milliseconds / 1000)

    # interaction
    def _require(self, selector):
        if selector not in self.selectors:
            raise ElementNotFound(selector)

    def fill(self, selector, value, timeout_ms):
        self._require(selector)
        if self.fill_error is not None:
            raise self.fill_error
        self.filled.append((selector, value))

    def set_checkbox(self, selector, checked, timeout_ms):
        self._require(selector)
        self.checked.append((selector, checked))

    def select_option(self, selector, value, timeout_ms):
        self._require(selector)
        self.selected.append((selector, value))

    def begin_submission(self, policy=None):
        self.submission_policy = policy
        self.submission_events = []

    def submission_observed(self):
        return bool(self.submission_events)

    def submission_refused(self, outcome):
        for event in self.submission_events:
            if event['outcome'] == outcome:
                return event
        return None

    def click(self, selector, timeout_ms):
        self._require(selector)
        if self.click_error is not None:
            raise self.click_error
        self.clicked.append(selector)

        if self.submits_network:
            # Model what the real gate does with the submission request.
            if self.submission_policy is not None:
                allowed, reason = self.submission_policy(self.submission_url)
                if not allowed:
                    self.submission_events.append(
                        {'url': self.submission_url, 'outcome': 'refused', 'reason': reason}
                    )
                    return
            if self.submission_blocked:
                self.submission_events.append(
                    {
                        'url': self.submission_url,
                        'outcome': 'blocked',
                        'reason': '10.0.0.5 is a private address.',
                    }
                )
                return
            self.submission_events.append(
                {'url': self.submission_url, 'outcome': 'sent', 'reason': None}
            )

        if self.on_submit is not None:
            self.on_submit(self)

    # helpers the on_submit callbacks use
    def show(self, text=None, selector=None, url=None):
        if text is not None:
            self.text = text
        if selector is not None:
            self.selectors.append(selector)
        if url is not None:
            self._final_url = url


def plan(**overrides):
    data = {
        'submit_selector': 'button[type=submit]',
        'fields': (FieldAction('#name', FlowFieldType.TEXT, 'Uptora Monitor'),),
        'success_text': 'Thanks',
    }
    data.update(overrides)
    return ContactFormPlan(**data)


def run(session, flow_plan=None, timeout_seconds=1):
    """A one-second budget keeps the suite quick while still exercising the
    real deadline arithmetic; the failure paths are what use it up."""
    return run_flow_check(
        url=URL,
        timeout_seconds=timeout_seconds,
        now=None,
        plan=flow_plan or plan(),
        session_factory=session.factory(),
    )


def submits_with(text=None, selector=None, url=None):
    def handler(session):
        session.show(text=text, selector=selector, url=url)

    return handler


class TestFieldInteraction:
    def test_a_text_field_is_filled(self):
        session = FakeSession(on_submit=submits_with(text='Thanks for your message'))

        outcome = run(session)

        assert outcome.is_success, outcome.error_message
        assert session.filled == [('#name', 'Uptora Monitor')]

    def test_an_email_field_is_filled(self):
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        run(
            session,
            plan(fields=(FieldAction('#email', FlowFieldType.EMAIL, 'monitor@example.com'),)),
        )

        assert session.filled == [('#email', 'monitor@example.com')]

    def test_a_textarea_is_filled(self):
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        run(
            session,
            plan(fields=(FieldAction('#message', FlowFieldType.TEXTAREA, 'Hello there'),)),
        )

        assert session.filled == [('#message', 'Hello there')]

    def test_fields_are_applied_in_configured_order(self):
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        run(
            session,
            plan(
                fields=(
                    FieldAction('#name', FlowFieldType.TEXT, 'one'),
                    FieldAction('#email', FlowFieldType.EMAIL, 'two'),
                    FieldAction('#message', FlowFieldType.TEXTAREA, 'three'),
                )
            ),
        )

        assert [selector for selector, _ in session.filled] == ['#name', '#email', '#message']

    @pytest.mark.parametrize(
        ('value', 'expected'),
        [('true', True), ('1', True), ('yes', True), ('', False), ('false', False)],
    )
    def test_a_checkbox_reads_its_value(self, value, expected):
        session = FakeSession(
            selectors=('#consent', 'button[type=submit]'),
            on_submit=submits_with(text='Thanks'),
        )

        run(session, plan(fields=(FieldAction('#consent', FlowFieldType.CHECKBOX, value),)))

        assert session.checked == [('#consent', expected)]

    def test_a_select_chooses_its_option(self):
        session = FakeSession(
            selectors=('#topic', 'button[type=submit]'),
            on_submit=submits_with(text='Thanks'),
        )

        run(session, plan(fields=(FieldAction('#topic', FlowFieldType.SELECT, 'sales'),)))

        assert session.selected == [('#topic', 'sales')]

    def test_the_submit_control_is_clicked(self):
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        run(session)

        assert session.clicked == ['button[type=submit]']

    def test_a_missing_field_is_reported_by_selector(self):
        session = FakeSession(selectors=('button[type=submit]',))

        outcome = run(session)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.FLOW_FIELD_NOT_FOUND
        assert '#name' in outcome.error_message
        assert session.clicked == []

    def test_a_field_interaction_error_is_distinguished(self):
        session = FakeSession(fill_error=InteractionFailed('#name: disabled'))

        outcome = run(session)

        assert outcome.error_type == ErrorType.FLOW_FIELD_INTERACTION_ERROR
        assert '#name' in outcome.error_message

    def test_a_missing_submit_control_is_reported(self):
        session = FakeSession(selectors=('#name',))

        outcome = run(session)

        assert outcome.error_type == ErrorType.FLOW_SUBMIT_NOT_FOUND
        assert 'button[type=submit]' in outcome.error_message

    def test_a_submit_failure_is_distinguished(self):
        session = FakeSession(click_error=InteractionFailed('button: intercepted'))

        outcome = run(session)

        assert outcome.error_type == ErrorType.FLOW_SUBMIT_ERROR


class TestSensitiveValues:
    """Configured values may be a real person's details. They stay out of results."""

    def test_a_missing_field_never_quotes_the_value(self):
        session = FakeSession(selectors=('button[type=submit]',))

        outcome = run(
            session,
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, SENSITIVE_VALUE),)),
        )

        assert SENSITIVE_VALUE not in outcome.error_message
        assert 'priya@example.com' not in outcome.error_message

    def test_an_interaction_error_never_quotes_the_value(self):
        session = FakeSession(fill_error=InteractionFailed('#name: not editable'))

        outcome = run(
            session,
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, SENSITIVE_VALUE),)),
        )

        assert SENSITIVE_VALUE not in outcome.error_message

    def test_an_assertion_failure_never_quotes_field_values(self):
        session = FakeSession(on_submit=submits_with(text='Something went wrong'))

        outcome = run(
            session,
            plan(fields=(FieldAction('#name', FlowFieldType.TEXT, SENSITIVE_VALUE),)),
        )

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING
        assert SENSITIVE_VALUE not in outcome.error_message


class TestSuccessSemantics:
    def test_full_navigation_success(self):
        session = FakeSession(
            on_submit=submits_with(text='Thanks for your message', url=f'{URL}/thank-you')
        )

        outcome = run(
            session,
            plan(success_text='Thanks', success_url_contains='/thank-you'),
        )

        assert outcome.is_success, outcome.error_message
        assert outcome.final_url == f'{URL}/thank-you'

    def test_same_page_ajax_success(self):
        """No navigation at all: the panel simply appears."""
        session = FakeSession(on_submit=submits_with(text='Thanks', selector='.success-message'))

        outcome = run(
            session,
            plan(success_text='Thanks', success_selector='.success-message'),
        )

        assert outcome.is_success, outcome.error_message
        assert outcome.final_url == URL

    def test_success_text_missing_fails(self):
        session = FakeSession(on_submit=submits_with(text='Please try again'))

        outcome = run(session)

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING

    def test_success_selector_present_passes(self):
        session = FakeSession(on_submit=submits_with(selector='.done'))

        outcome = run(session, plan(success_text=None, success_selector='.done'))

        assert outcome.is_success, outcome.error_message

    def test_success_selector_missing_fails(self):
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        outcome = run(session, plan(success_text=None, success_selector='.never-appears'))

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_SELECTOR_MISSING

    def test_success_url_matching_passes(self):
        session = FakeSession(on_submit=submits_with(url='https://example.com/thanks'))

        outcome = run(session, plan(success_text=None, success_url_contains='/thanks'))

        assert outcome.is_success, outcome.error_message

    def test_success_url_mismatch_fails(self):
        session = FakeSession(on_submit=submits_with(url='https://example.com/error'))

        outcome = run(session, plan(success_text=None, success_url_contains='/thanks'))

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_URL_MISMATCH

    def test_two_assertions_use_and_semantics(self):
        """Text passes, selector does not. The flow must still fail."""
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        outcome = run(session, plan(success_text='Thanks', success_selector='.never'))

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.FLOW_SUCCESS_SELECTOR_MISSING

    def test_three_assertions_all_pass(self):
        session = FakeSession(
            on_submit=submits_with(
                text='Thanks for your message',
                selector='.success-message',
                url='https://example.com/thank-you',
            )
        )

        outcome = run(
            session,
            plan(
                success_text='Thanks',
                success_selector='.success-message',
                success_url_contains='/thank-you',
            ),
        )

        assert outcome.is_success, outcome.error_message

    def test_three_assertions_fail_if_only_the_url_is_wrong(self):
        session = FakeSession(
            on_submit=submits_with(text='Thanks', selector='.success-message', url=URL)
        )

        outcome = run(
            session,
            plan(
                success_text='Thanks',
                success_selector='.success-message',
                success_url_contains='/thank-you',
            ),
        )

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_URL_MISMATCH


class TestWaiting:
    def test_a_late_success_is_still_caught(self):
        """The panel appears on the third poll, not immediately."""
        state = {'polls': 0}

        session = FakeSession()

        def slow_sleep(milliseconds):
            state['polls'] += 1
            if state['polls'] >= 3:
                session.text = 'Thanks'

        session.sleep = slow_sleep
        session.on_submit = lambda s: None

        outcome = run(session)

        assert outcome.is_success, outcome.error_message
        assert state['polls'] >= 3

    def test_polling_stops_at_the_budget(self):
        session = FakeSession(on_submit=submits_with(text='still working'))

        outcome = run(session, timeout_seconds=1)

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING


class TestPageFailures:
    def test_a_page_that_does_not_load_never_submits(self):
        session = FakeSession(navigate_error=NavigationFailed('net::ERR_NAME_NOT_RESOLVED'))

        outcome = run(session)

        assert outcome.error_type == ErrorType.DNS_ERROR
        assert session.clicked == []
        assert session.filled == []

    def test_an_error_status_never_submits(self):
        session = FakeSession(status=500)

        outcome = run(session)

        assert outcome.error_type == ErrorType.HTTP_ERROR
        assert session.clicked == []

    def test_a_navigation_timeout_is_reported(self):
        session = FakeSession(navigate_error=BrowserTimeout('Timeout 10000ms exceeded'))

        outcome = run(session)

        assert outcome.error_type == ErrorType.BROWSER_TIMEOUT

    def test_a_page_resting_somewhere_private_never_submits(self):
        session = FakeSession(final_url='http://169.254.169.254/latest/')

        outcome = run(session)

        assert outcome.error_type == ErrorType.BLOCKED_TARGET
        assert session.clicked == []

    def test_the_session_always_closes(self):
        session = FakeSession(selectors=())

        run(session)

        assert session.closed


class TestEvidence:
    def test_a_failed_flow_captures_a_screenshot(self):
        session = FakeSession(on_submit=submits_with(text='nope'))

        outcome = run(session)

        assert not outcome.is_success
        assert outcome.screenshot == SCREENSHOT

    def test_a_successful_flow_captures_none(self):
        session = FakeSession(on_submit=submits_with(text='Thanks'))

        outcome = run(session)

        assert outcome.is_success
        assert outcome.screenshot is None
        assert session.screenshots_taken == 0

    def test_a_screenshot_failure_does_not_replace_the_flow_error(self):
        session = FakeSession(on_submit=submits_with(text='nope'))

        def explode():
            raise RuntimeError('display gone')

        session.screenshot = explode

        outcome = run(session)

        assert outcome.error_type == ErrorType.FLOW_SUCCESS_TEXT_MISSING
        assert outcome.screenshot is None

    def test_every_failure_stage_captures_evidence(self):
        stages = [
            (FakeSession(selectors=('button[type=submit]',)), ErrorType.FLOW_FIELD_NOT_FOUND),
            (FakeSession(selectors=('#name',)), ErrorType.FLOW_SUBMIT_NOT_FOUND),
            (FakeSession(on_submit=submits_with(text='nope')), ErrorType.FLOW_SUCCESS_TEXT_MISSING),
        ]
        for session, expected in stages:
            outcome = run(session)

            assert outcome.error_type == expected
            assert outcome.screenshot == SCREENSHOT


class TestSubmissionEvidence:
    """A confirmation that was already on screen proves nothing on its own."""

    def test_pre_existing_success_text_is_not_a_success(self):
        session = FakeSession(text='Thanks for your message', submits_network=False)

        outcome = run(session)

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.FLOW_SUBMIT_ERROR

    def test_pre_existing_success_selector_is_not_a_success(self):
        session = FakeSession(
            selectors=('#name', 'button[type=submit]', '.success-message'),
            submits_network=False,
        )

        outcome = run(session, plan(success_text=None, success_selector='.success-message'))

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.FLOW_SUBMIT_ERROR

    def test_a_url_condition_already_true_is_not_a_success(self):
        session = FakeSession(final_url='https://example.com/contact/thanks', submits_network=False)

        outcome = run(session, plan(success_text=None, success_url_contains='/thanks'))

        assert not outcome.is_success
        assert outcome.error_type == ErrorType.FLOW_SUBMIT_ERROR

    def test_every_assertion_already_true_and_nothing_submitted_still_fails(self):
        session = FakeSession(
            text='Thanks for your message',
            selectors=('#name', 'button[type=submit]', '.success-message'),
            final_url='https://example.com/thanks',
            submits_network=False,
        )

        outcome = run(
            session,
            plan(
                success_text='Thanks',
                success_selector='.success-message',
                success_url_contains='/thanks',
            ),
        )

        assert outcome.error_type == ErrorType.FLOW_SUBMIT_ERROR

    def test_a_real_submission_with_pre_existing_text_succeeds(self):
        """The confirmation was already there, but a submission really happened."""
        session = FakeSession(text='Thanks for your message')

        outcome = run(session)

        assert outcome.is_success, outcome.error_message

    def test_a_purely_client_side_change_counts_as_evidence(self):
        """No request, but the click visibly changed the page."""
        session = FakeSession(submits_network=False, on_submit=submits_with(text='Thanks'))

        outcome = run(session)

        assert outcome.is_success, outcome.error_message

    def test_a_post_submit_appearance_succeeds(self):
        session = FakeSession(on_submit=submits_with(text='Thanks', selector='.success-message'))

        outcome = run(session, plan(success_text='Thanks', success_selector='.success-message'))

        assert outcome.is_success, outcome.error_message


class TestSubmissionRefusals:
    def test_a_blocked_submission_fails_as_a_blocked_target(self):
        session = FakeSession(submission_blocked=True, text='Thanks for your message')

        outcome = run(session)

        # The confirmation text is present, but the submission never left.
        assert not outcome.is_success
        assert outcome.error_type == ErrorType.BLOCKED_TARGET

    def test_a_refused_destination_is_named_distinctly(self):
        session = FakeSession(
            submission_url='https://unrelated.example/spam',
            text='Thanks for your message',
        )

        outcome = run(session)

        assert outcome.error_type == ErrorType.FLOW_DESTINATION_NOT_ALLOWED
        assert 'unrelated.example' in outcome.error_message

    def test_an_apex_to_www_submission_is_permitted(self):
        session = FakeSession(
            submission_url='https://www.example.com/submit',
            on_submit=submits_with(text='Thanks'),
        )

        outcome = run(session)

        assert outcome.is_success, outcome.error_message

    def test_an_unrelated_blocked_subresource_does_not_fail_the_flow(self):
        """Background noise is not the submission."""
        session = FakeSession(on_submit=submits_with(text='Thanks'))
        session.diagnostics.record_blocked(
            'http://169.254.169.254/pixel.png', 'link-local', navigation=False
        )

        outcome = run(session)

        assert outcome.is_success, outcome.error_message
