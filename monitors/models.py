import secrets

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

# Minimums exist so a user cannot configure Uptora into hammering a target or
# holding a worker open indefinitely.
#
#   interval: 60s .. 24h   - one check a minute is the tightest schedule we are
#                            willing to run per monitor.
#   timeout:  1s  .. 30s   - deliberately below the minimum interval, so a check
#                            always finishes before the next one is due and runs
#                            can never pile up on top of each other.
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_INTERVAL_SECONDS = 300

MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 10


class MonitorType(models.TextChoices):
    """Kinds of check Uptora can run.

    The API rejects anything not listed, so a new member is the single place
    that has to change.
    """

    HTTP = 'HTTP', 'HTTP'
    BROWSER = 'BROWSER', 'Browser'
    FLOW = 'FLOW', 'Flow'


class ErrorType(models.TextChoices):
    """Stable, machine-readable failure taxonomy.

    These values are written to the database and will be read by alerting rules,
    so treat them as an API: add members, never rename or repurpose them.
    """

    TIMEOUT = 'TIMEOUT', 'Timeout'
    DNS_ERROR = 'DNS_ERROR', 'DNS error'
    CONNECTION_ERROR = 'CONNECTION_ERROR', 'Connection error'
    TLS_ERROR = 'TLS_ERROR', 'TLS error'
    HTTP_ERROR = 'HTTP_ERROR', 'HTTP error'
    TOO_MANY_REDIRECTS = 'TOO_MANY_REDIRECTS', 'Too many redirects'
    BLOCKED_TARGET = 'BLOCKED_TARGET', 'Blocked target'

    # Browser checks. Network-level failures reuse the members above, because a
    # DNS failure is the same fact however it was observed.
    BROWSER_TIMEOUT = 'BROWSER_TIMEOUT', 'Browser timeout'
    NAVIGATION_ERROR = 'NAVIGATION_ERROR', 'Navigation error'
    EXPECTED_TEXT_MISSING = 'EXPECTED_TEXT_MISSING', 'Expected text missing'
    EXPECTED_SELECTOR_MISSING = 'EXPECTED_SELECTOR_MISSING', 'Expected selector missing'
    BROWSER_ERROR = 'BROWSER_ERROR', 'Browser error'

    # User flows. Each names the step that failed, so an operator can tell a
    # broken form apart from a broken page without opening the screenshot.
    FLOW_CONFIGURATION_ERROR = 'FLOW_CONFIGURATION_ERROR', 'Flow configuration error'
    FLOW_FIELD_NOT_FOUND = 'FLOW_FIELD_NOT_FOUND', 'Flow field not found'
    FLOW_FIELD_INTERACTION_ERROR = 'FLOW_FIELD_INTERACTION_ERROR', 'Flow field interaction error'
    FLOW_SUBMIT_NOT_FOUND = 'FLOW_SUBMIT_NOT_FOUND', 'Flow submit control not found'
    FLOW_SUBMIT_ERROR = 'FLOW_SUBMIT_ERROR', 'Flow submit error'
    FLOW_TIMEOUT = 'FLOW_TIMEOUT', 'Flow timeout'
    FLOW_SUCCESS_TEXT_MISSING = 'FLOW_SUCCESS_TEXT_MISSING', 'Flow success text missing'
    FLOW_SUCCESS_SELECTOR_MISSING = (
        'FLOW_SUCCESS_SELECTOR_MISSING',
        'Flow success selector missing',
    )
    FLOW_SUCCESS_URL_MISMATCH = 'FLOW_SUCCESS_URL_MISMATCH', 'Flow success URL mismatch'
    FLOW_DESTINATION_NOT_ALLOWED = (
        'FLOW_DESTINATION_NOT_ALLOWED',
        'Flow submission destination not allowed',
    )

    UNKNOWN_ERROR = 'UNKNOWN_ERROR', 'Unknown error'


class FlowKind(models.TextChoices):
    """Which user journey a FLOW monitor walks.

    Only the contact form exists today. LOGIN, SEARCH and the rest join this
    enum with their own plan builder and executor under checks/flows/; nothing
    in the monitoring engine has to change to admit one.
    """

    CONTACT_FORM = 'CONTACT_FORM', 'Contact form'


class FlowFieldType(models.TextChoices):
    """How a configured value is applied to a control.

    A closed set of declarative actions. There is deliberately no member that
    runs script: a flow describes what to type where, never what to execute.
    """

    TEXT = 'TEXT', 'Text'
    EMAIL = 'EMAIL', 'Email'
    TEXTAREA = 'TEXTAREA', 'Textarea'
    CHECKBOX = 'CHECKBOX', 'Checkbox'
    SELECT = 'SELECT', 'Select'


class Monitor(models.Model):
    """A recurring check configured against one website.

    Ownership is derived from the website rather than stored again here, so
    there is exactly one place a row's owner can come from.
    """

    website = models.ForeignKey(
        'websites.Website',
        on_delete=models.CASCADE,
        related_name='monitors',
    )
    monitor_type = models.CharField(
        max_length=16,
        choices=MonitorType.choices,
        default=MonitorType.HTTP,
    )
    is_enabled = models.BooleanField(default=True)
    interval_seconds = models.PositiveIntegerField(
        default=DEFAULT_INTERVAL_SECONDS,
        validators=[
            MinValueValidator(MIN_INTERVAL_SECONDS),
            MaxValueValidator(MAX_INTERVAL_SECONDS),
        ],
    )
    timeout_seconds = models.PositiveIntegerField(
        default=DEFAULT_TIMEOUT_SECONDS,
        validators=[
            MinValueValidator(MIN_TIMEOUT_SECONDS),
            MaxValueValidator(MAX_TIMEOUT_SECONDS),
        ],
    )
    # Browser expectations. Two plain nullable fields rather than a JSON blob:
    # there are only two of them, they are queryable, and a schema change is a
    # visible migration instead of a silently reshaped document.
    #
    # Both are optional. A browser check with neither set still means something
    # useful: the page loaded and rendered without a fatal error. HTTP monitors
    # simply never read these, so leaving them set on one is harmless.
    expected_text = models.CharField(max_length=200, null=True, blank=True)
    expected_selector = models.CharField(max_length=200, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at', '-id')

    def __str__(self):
        return f'{self.get_monitor_type_display()} monitor for {self.website.url}'

    @property
    def owner(self):
        """Convenience accessor. The website is the single source of ownership."""
        return self.website.owner


def screenshot_path(instance, filename):
    """Give every screenshot an unguessable name.

    The stored name ignores whatever the caller passed: a sequential or
    predictable path would let anyone who can reach the media store enumerate
    other customers' failure screenshots.
    """
    return f'check-screenshots/{timezone.now():%Y/%m}/{secrets.token_urlsafe(24)}.png'


class CheckResult(models.Model):
    """One recorded execution of a monitor.

    Conceptually immutable: rows are appended, never edited. Nothing in the API
    updates a result, and the history is only ever read newest-first.
    """

    monitor = models.ForeignKey(
        Monitor,
        on_delete=models.CASCADE,
        related_name='results',
    )
    checked_at = models.DateTimeField(default=timezone.now)
    is_success = models.BooleanField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)
    error_type = models.CharField(
        max_length=32,
        choices=ErrorType.choices,
        null=True,
        blank=True,
    )
    # Capped on purpose: a concise summary, never a stack trace.
    error_message = models.CharField(max_length=500, null=True, blank=True)
    # Where the check actually ended up, after any redirects.
    final_url = models.URLField(max_length=500, null=True, blank=True)
    # Evidence for a failure, stored through Django's storage API so moving to
    # S3 is a settings change. Never populated for a successful check.
    screenshot = models.FileField(
        upload_to=screenshot_path,
        max_length=255,
        null=True,
        blank=True,
    )
    ssl_expires_at = models.DateTimeField(null=True, blank=True)
    # Signed, so an already-expired certificate reads as a negative number.
    ssl_days_remaining = models.IntegerField(null=True, blank=True)
    # Pipeline bookkeeping, not part of the observation: stamped once the
    # incident engine has consumed this result. What the check saw stays
    # immutable; this only records that it has been acted on, and it is what
    # makes reprocessing the same result a no-op.
    incident_processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-checked_at', '-id')
        indexes = [
            # The access pattern is always "latest results for this monitor".
            models.Index(fields=['monitor', '-checked_at'], name='checkresult_monitor_time'),
            # Supports scanning recent failures across all monitors.
            models.Index(fields=['is_success', '-checked_at'], name='checkresult_success_time'),
        ]

    def __str__(self):
        outcome = 'up' if self.is_success else f'down ({self.error_type})'
        return f'{self.monitor_id} {outcome} at {self.checked_at:%Y-%m-%d %H:%M:%S}'


class FlowConfig(models.Model):
    """Declarative configuration for a FLOW monitor.

    Relational columns rather than a JSON document: the fields are few, fixed
    and queryable, and a schema change is a visible migration. Nothing here can
    describe code, and nothing here names a destination -- see below.

    Anti-abuse: there is deliberately no url or action column. A flow always
    navigates to its website's own URL and submits whatever form that page
    contains, so configuration cannot aim Uptora at an arbitrary endpoint. The
    monitored site's own HTML decides where its form posts, exactly as it would
    for a human visitor. Without that constraint this model would be a
    programmable request sender.

    Values are treated as potentially sensitive: they are never written into a
    CheckResult message or diagnostics. See FlowField.value.
    """

    monitor = models.OneToOneField(
        Monitor,
        on_delete=models.CASCADE,
        related_name='flow_config',
    )
    flow_kind = models.CharField(max_length=32, choices=FlowKind.choices)

    submit_selector = models.CharField(max_length=500)

    # Success assertions. All configured ones must hold; at least one must be
    # set, enforced below. A form that was submitted with nothing checked
    # afterwards proves only that a button was clickable.
    success_text = models.CharField(max_length=200, null=True, blank=True)
    success_selector = models.CharField(max_length=500, null=True, blank=True)
    success_url_contains = models.CharField(max_length=500, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(success_text__isnull=False)
                    | models.Q(success_selector__isnull=False)
                    | models.Q(success_url_contains__isnull=False)
                ),
                name='flowconfig_requires_a_success_assertion',
            )
        ]

    def __str__(self):
        return f'{self.get_flow_kind_display()} flow for monitor {self.monitor_id}'


class FlowField(models.Model):
    """One control a flow fills in, and what to put in it."""

    flow_config = models.ForeignKey(
        FlowConfig,
        on_delete=models.CASCADE,
        related_name='fields',
    )
    selector = models.CharField(max_length=500)
    field_type = models.CharField(
        max_length=16,
        choices=FlowFieldType.choices,
        default=FlowFieldType.TEXT,
    )
    # Potentially sensitive. Readable by the owner through the API, but never
    # copied into an error message, diagnostics or incident metadata: a failure
    # names the selector it could not fill, never what it was going to type.
    # When login and payment flows arrive, this is the column that moves behind
    # encryption or a secret store; keeping it isolated here is what makes that
    # a contained change.
    value = models.CharField(max_length=1000, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('position', 'id')

    def __str__(self):
        return f'{self.field_type} {self.selector}'
