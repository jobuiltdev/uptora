"""Email bodies.

Rendered entirely from the NotificationEvent snapshot. Nothing here reads an
Incident, a Monitor, a Website or a CheckResult, which matters for three
reasons:

  * a delivery retried hours later reports the event as it was when it
    occurred, not as the world has since become;
  * a notification survives the withdrawal of the incident that prompted it,
    because there is nothing live left to fetch;
  * there is exactly one place -- the snapshot -- where the question "could
    customer data leak into an email" has to be answered.

What is deliberately absent is as important as what is present. No configured
flow values, no submitted form contents, no lease or run identifiers, no
tracebacks, and no media URL for a failure screenshot: serving those behind
authentication is still deferred, so the email says evidence exists rather than
handing out an unauthenticated link.
"""

from html import escape as html_escape
from urllib.parse import urlsplit

from django.conf import settings

from notifications.models import EventType


def display_host(event):
    """The site as a person refers to it, for a subject line."""
    return urlsplit(event.website_url).hostname or event.website_name or 'your site'


def humanize_duration(seconds):
    if seconds is None:
        return 'unknown'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} seconds'
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m {remainder}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes}m'


def moment(value):
    return f'{value:%Y-%m-%d %H:%M:%S UTC}' if value else 'unknown'


def dashboard_hint():
    """Where the details will live. A base URL, not a guessable deep link."""
    return settings.APP_BASE_URL.rstrip('/')


def render(event):
    """Return (subject, text_body, html_body) for an event."""
    if event.event_type == EventType.INCIDENT_OPENED:
        return render_outage(event)
    return render_recovery(event)


def render_outage(event):
    subject = f'[Uptora] Problem detected on {display_host(event)}'

    evidence = (
        'A screenshot of the failure was captured and is available in Uptora.'
        if event.has_evidence
        else ''
    )

    lines = [
        f'Uptora detected a problem with {event.website_name}.',
        '',
        f'Website:  {event.website_name}',
        f'URL:      {event.website_url}',
        f'Check:    {event.monitor_type}',
        f'Started:  {moment(event.incident_started_at)}',
        f'Problem:  {event.failure_summary}',
        '',
        'Uptora confirmed this after two consecutive failed checks, so it is',
        'not a single blip. You will get one more email when it recovers.',
        '',
        f'Details: {dashboard_hint()}',
    ]
    if evidence:
        lines.insert(-2, evidence)
        lines.insert(-2, '')

    html_body = html_document(
        heading=f'Problem detected on {display_host(event)}',
        intro=f'Uptora detected a problem with <strong>{escape(event.website_name)}</strong>.',
        rows=[
            ('Website', escape(event.website_name)),
            ('URL', escape(event.website_url)),
            ('Check', escape(event.monitor_type)),
            ('Started', moment(event.incident_started_at)),
            ('Problem', escape(event.failure_summary)),
        ],
        footer=(
            'Confirmed after two consecutive failed checks. '
            'You will get one more email when it recovers.'
        ),
        extra=escape(evidence) if evidence else None,
    )
    return subject, '\n'.join(lines), html_body


def render_recovery(event):
    subject = f'[Uptora] {display_host(event)} has recovered'

    duration = None
    if event.incident_started_at and event.incident_resolved_at:
        duration = (event.incident_resolved_at - event.incident_started_at).total_seconds()

    text_body = '\n'.join(
        [
            f'{event.website_name} is responding normally again.',
            '',
            f'Website:   {event.website_name}',
            f'URL:       {event.website_url}',
            f'Started:   {moment(event.incident_started_at)}',
            f'Recovered: {moment(event.incident_resolved_at)}',
            f'Duration:  {humanize_duration(duration)}',
            f'Failed checks: {event.failure_count}',
            '',
            f'Details: {dashboard_hint()}',
        ]
    )

    html_body = html_document(
        heading=f'{display_host(event)} has recovered',
        intro=f'<strong>{escape(event.website_name)}</strong> is responding normally again.',
        rows=[
            ('Website', escape(event.website_name)),
            ('URL', escape(event.website_url)),
            ('Started', moment(event.incident_started_at)),
            ('Recovered', moment(event.incident_resolved_at)),
            ('Duration', humanize_duration(duration)),
            ('Failed checks', str(event.failure_count)),
        ],
        footer='Confirmed after two consecutive successful checks.',
    )
    return subject, text_body, html_body


def escape(value):
    return html_escape(str(value))


def html_document(heading, intro, rows, footer, extra=None):
    """A plain, readable message. Not a marketing template."""
    cells = '\n'.join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#666;">{label}</td>'
        f'<td style="padding:4px 0;">{value}</td></tr>'
        for label, value in rows
    )
    extra_block = f'<p>{extra}</p>' if extra else ''
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#111;">'
        f'<h2 style="margin:0 0 12px;font-size:18px;">{escape(heading)}</h2>'
        f'<p style="margin:0 0 16px;">{intro}</p>'
        f'<table style="border-collapse:collapse;margin:0 0 16px;">{cells}</table>'
        f'{extra_block}'
        f'<p style="color:#666;font-size:13px;margin:16px 0 0;">{escape(footer)}</p>'
        f'<p style="font-size:13px;margin:8px 0 0;">'
        f'<a href="{escape(dashboard_hint())}">Open Uptora</a></p>'
        '</div>'
    )
