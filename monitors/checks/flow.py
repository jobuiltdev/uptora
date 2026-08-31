"""User-flow checks.

A flow walks a real journey through a real browser: fill the controls, submit,
and prove the site accepted it. It reuses the browser session and its request
gate wholesale, so a flow's writes are subject to exactly the same network
policy as a browser check's reads.

The database stops at the door. build_plan turns configuration into a plain
dataclass, and the executors work only from that, which keeps them free of
Django and directly testable.
"""

import time

from monitors.checks.base import CheckOutcome
from monitors.checks.browser import BrowserTimeout, default_session_factory
from monitors.checks.flows.contact_form import ContactFormPlan, FieldAction, run_contact_form
from monitors.checks.flows.destination import build_submission_policy
from monitors.models import ErrorType, FlowKind
from monitors.ssrf import BlockedTargetError, TargetResolutionError, resolve_target


class FlowConfigurationError(Exception):
    """The monitor's flow configuration is missing or unusable."""


def build_plan(monitor):
    """Read a monitor's flow configuration into a plain plan.

    Raises FlowConfigurationError rather than returning something half-formed:
    a flow with no configuration has nothing to prove and must not silently
    pass.
    """
    config = getattr(monitor, 'flow_config', None)
    if config is None:
        raise FlowConfigurationError('This flow monitor has no configuration.')

    if config.flow_kind != FlowKind.CONTACT_FORM:
        raise FlowConfigurationError(f'Unsupported flow kind {config.flow_kind!r}.')

    plan = ContactFormPlan(
        submit_selector=config.submit_selector,
        fields=tuple(
            FieldAction(
                selector=field.selector,
                field_type=field.field_type,
                value=field.value,
            )
            for field in config.fields.all()
        ),
        success_text=config.success_text or None,
        success_selector=config.success_selector or None,
        success_url_contains=config.success_url_contains or None,
    )

    if not plan.has_assertion():
        raise FlowConfigurationError('This flow has no success assertion configured.')
    if not plan.submit_selector:
        raise FlowConfigurationError('This flow has no submit selector configured.')

    return plan


def run_flow_check(
    url,
    timeout_seconds,
    now,
    plan,
    session_factory=None,
    launch_args=(),
):
    """Walk `plan` against `url` in a browser and describe what happened.

    `url` is always the monitored website's own URL. Nothing in a plan can name
    a destination, so a flow cannot be pointed at an endpoint of the
    configurer's choosing, and the submission itself is held to the same site as
    that URL so the page's own HTML cannot aim it elsewhere either.
    """
    started = time.perf_counter()

    def elapsed_ms():
        return int((time.perf_counter() - started) * 1000)

    # Checked before launching, so an obviously forbidden target never costs a
    # browser. The route gate re-checks it anyway, along with every redirect.
    try:
        resolve_target(url)
    except BlockedTargetError as exc:
        return CheckOutcome.failure(ErrorType.BLOCKED_TARGET, exc)
    except TargetResolutionError as exc:
        return CheckOutcome.failure(ErrorType.DNS_ERROR, exc, response_time_ms=elapsed_ms())

    factory = session_factory or default_session_factory
    timeout_ms = timeout_seconds * 1000

    submission_policy = build_submission_policy(url)

    try:
        with factory(timeout_ms, launch_args) as session:
            return run_contact_form(
                session,
                plan,
                url,
                timeout_ms,
                elapsed_ms,
                submission_policy=submission_policy,
            )
    except BrowserTimeout as exc:
        return CheckOutcome.failure(ErrorType.FLOW_TIMEOUT, exc, response_time_ms=elapsed_ms())
    except Exception as exc:  # noqa: BLE001 - a bad page must not kill the runner
        return CheckOutcome.failure(ErrorType.BROWSER_ERROR, exc, response_time_ms=elapsed_ms())
