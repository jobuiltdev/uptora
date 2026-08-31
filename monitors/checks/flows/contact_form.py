"""The contact-form flow.

Fill the configured controls, submit, and prove the submission was accepted.

Everything here works against the session interface, so it is exercised in unit
tests with a fake and against real Chromium in the integration suite. It holds
no Playwright import and touches no database.

Submission waiting
------------------
Forms finish in three different ways: a full-page navigation, an AJAX request
that swaps in a thank-you panel, or a purely client-side UI change. Nothing
distinguishes them from outside, so the strategy does not try to guess.

After the click, each configured assertion is waited for against the *remaining*
timeout budget:

  * a selector uses the session's native selector wait, which resolves whether
    it appears through navigation or a DOM update;
  * text and URL have no native equivalent matching our documented semantics
    (substring of rendered text; substring of the redirect-resolved URL), so
    they are polled against a deadline with a short tick.

The tick is a yield between condition checks, not the synchronisation itself:
the loop ends the moment the condition holds, and the deadline is derived from
the monitor's own timeout so the whole check stays bounded.
"""

import time
from dataclasses import dataclass

from monitors.checks.base import CheckOutcome
from monitors.checks.browser import ElementNotFound, InteractionFailed, failed, load_page
from monitors.models import ErrorType, FlowFieldType

# How long to wait between condition checks while a submission settles.
POLL_INTERVAL_MS = 200

# Truthy spellings for a checkbox value, so configuration can say what it means.
CHECKED_VALUES = frozenset({'1', 'true', 'yes', 'on', 'checked'})


@dataclass(frozen=True)
class FieldAction:
    """One control to touch, and what to put in it."""

    selector: str
    field_type: str
    value: str


@dataclass(frozen=True)
class ContactFormPlan:
    """Everything the executor needs, with no model objects attached.

    Built from FlowConfig by the caller, so this module stays database-free and
    the whole flow can be driven from a plain dataclass in tests.
    """

    submit_selector: str
    fields: tuple
    success_text: str = None
    success_selector: str = None
    success_url_contains: str = None

    @property
    def assertions(self):
        return (self.success_text, self.success_selector, self.success_url_contains)

    def has_assertion(self):
        return any(self.assertions)


def apply_field(session, field, timeout_ms):
    """Perform one declarative action. Raises ElementNotFound/InteractionFailed."""
    if field.field_type == FlowFieldType.CHECKBOX:
        checked = field.value.strip().lower() in CHECKED_VALUES
        session.set_checkbox(field.selector, checked, timeout_ms)
    elif field.field_type == FlowFieldType.SELECT:
        session.select_option(field.selector, field.value, timeout_ms)
    else:
        # TEXT, EMAIL and TEXTAREA all fill the same way; the distinction is
        # documentation for whoever configured it, not different behaviour.
        session.fill(field.selector, field.value, timeout_ms)


def assertion_state(session, plan):
    """Which configured assertions currently hold.

    Captured before the click so a page that already says "Thanks" cannot be
    mistaken for one that just accepted a message.
    """
    state = {}
    if plan.success_text:
        state['text'] = session.has_text(plan.success_text)
    if plan.success_selector:
        # A near-zero budget: the question is whether it is there now, not
        # whether it turns up.
        state['selector'] = session.wait_for_selector(plan.success_selector, 0)
    if plan.success_url_contains:
        state['url'] = plan.success_url_contains in (session.current_url() or '')
    return state


def assertion_changed(session, plan, baseline):
    """True when something that was not satisfied before the click now is."""
    current = assertion_state(session, plan)
    return any(current.get(name) and not baseline.get(name) for name in current)


def submission_evidence(session, plan, baseline, deadline):
    """Wait for proof that clicking submit actually did something.

    Either the network shows a request we can attribute to the submission, or a
    success assertion that was false beforehand has become true. Without one of
    those, a page whose confirmation text was already on screen would pass
    without anything having been sent.
    """
    return wait_until(
        lambda: session.submission_observed() or assertion_changed(session, plan, baseline),
        session,
        deadline,
    )


def wait_until(condition, session, deadline):
    """Poll a condition until it holds or the budget runs out."""
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        session.sleep(POLL_INTERVAL_MS)


def run_contact_form(session, plan, url, timeout_ms, elapsed_ms, submission_policy=None):
    """Walk the flow and return a CheckOutcome.

    Failure messages name selectors, never configured values: those may be a
    real person's name or address and must not end up in a check history.
    """
    navigation, failure = load_page(session, url, timeout_ms, elapsed_ms)
    if failure is not None:
        return failure

    common = {'status_code': navigation.status, 'final_url': navigation.final_url}

    def remaining():
        return max(timeout_ms - elapsed_ms(), 0)

    for field in plan.fields:
        try:
            apply_field(session, field, remaining())
        except ElementNotFound as exc:
            return failed(
                session,
                ErrorType.FLOW_FIELD_NOT_FOUND,
                f'No element matched the configured field selector: {exc}',
                elapsed_ms(),
                **common,
            )
        except InteractionFailed as exc:
            return failed(
                session,
                ErrorType.FLOW_FIELD_INTERACTION_ERROR,
                f'Could not fill the configured field: {exc}',
                elapsed_ms(),
                **common,
            )

    baseline = assertion_state(session, plan)

    # The window opens immediately before the click, so only what the click
    # causes can be attributed to it.
    session.begin_submission(submission_policy)

    try:
        session.click(plan.submit_selector, remaining())
    except ElementNotFound as exc:
        return failed(
            session,
            ErrorType.FLOW_SUBMIT_NOT_FOUND,
            f'No element matched the submit selector: {exc}',
            elapsed_ms(),
            **common,
        )
    except InteractionFailed as exc:
        return failed(
            session,
            ErrorType.FLOW_SUBMIT_ERROR,
            f'Could not submit the form: {exc}',
            elapsed_ms(),
            **common,
        )

    return confirm_submission(session, plan, baseline, timeout_ms, elapsed_ms, common)


def confirm_submission(session, plan, baseline, timeout_ms, elapsed_ms, common):
    """Every configured assertion must hold. AND semantics, never OR.

    A form that shows a thank-you message but never reaches the confirmation
    URL has not been proven to work, so one satisfied assertion is not enough.

    Before any of that, the submission itself has to have happened and been
    allowed to leave.
    """
    deadline = time.monotonic() + max(timeout_ms - elapsed_ms(), 0) / 1000

    observed = submission_evidence(session, plan, baseline, deadline)

    refused = session.submission_refused('refused')
    if refused:
        return failed(
            session,
            ErrorType.FLOW_DESTINATION_NOT_ALLOWED,
            f'The form submission was not sent: {refused["reason"]}',
            elapsed_ms(),
            **common,
        )

    blocked = session.submission_refused('blocked')
    if blocked:
        return failed(
            session,
            ErrorType.BLOCKED_TARGET,
            f'The form submission was refused: {blocked["reason"]}',
            elapsed_ms(),
            **common,
        )

    if not observed:
        return failed(
            session,
            ErrorType.FLOW_SUBMIT_ERROR,
            'The submit control was clicked but nothing was submitted and the page did not change.',
            elapsed_ms(),
            **common,
        )

    if plan.success_selector:
        budget = max(int((deadline - time.monotonic()) * 1000), 0)
        if not session.wait_for_selector(plan.success_selector, budget):
            return failed(
                session,
                ErrorType.FLOW_SUCCESS_SELECTOR_MISSING,
                f'Submitted, but the success selector never appeared: {plan.success_selector!r}',
                elapsed_ms(),
                **common,
            )

    if plan.success_text:
        found = wait_until(lambda: session.has_text(plan.success_text), session, deadline)
        if not found:
            return failed(
                session,
                ErrorType.FLOW_SUCCESS_TEXT_MISSING,
                f'Submitted, but the success text never appeared: {plan.success_text!r}',
                elapsed_ms(),
                **common,
            )

    if plan.success_url_contains:
        fragment = plan.success_url_contains
        matched = wait_until(lambda: fragment in (session.current_url() or ''), session, deadline)
        if not matched:
            return failed(
                session,
                ErrorType.FLOW_SUCCESS_URL_MISMATCH,
                f'Submitted, but the URL never contained {fragment!r}',
                elapsed_ms(),
                **{**common, 'final_url': session.current_url()},
            )

    return CheckOutcome(
        is_success=True,
        response_time_ms=elapsed_ms(),
        status_code=common['status_code'],
        final_url=session.current_url() or common['final_url'],
    )
