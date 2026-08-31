"""Resend provider.

Uses the HTTP API through httpx, which the project already depends on, rather
than pulling in a vendor SDK for one POST.

An Idempotency-Key derived from the delivery id is sent with every message.
Where Resend honours it, a retry after a crash is deduplicated on their side.
We do not claim that as an end-to-end guarantee: see send_delivery for the
crash window it narrows but does not close.
"""

import httpx

from notifications.email.base import (
    EmailProvider,
    PermanentSendError,
    SendResult,
    TransientSendError,
)

API_URL = 'https://api.resend.com/emails'
TIMEOUT_SECONDS = 15

# 4xx that will never succeed on a retry: a bad address, a rejected sender, a
# malformed request. 429 is excluded because it explicitly asks us to wait.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 422})


class ResendEmailProvider(EmailProvider):
    def __init__(self, api_key, from_address, client=None):
        self.api_key = api_key
        self.from_address = from_address
        # Injectable so tests can drive the real request-building code against
        # a MockTransport without reaching the network.
        self._client = client

    def client(self):
        return self._client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def send(self, to, subject, text_body, html_body=None, idempotency_key=None):
        payload = {
            'from': self.from_address,
            'to': [to],
            'subject': subject,
            'text': text_body,
        }
        if html_body:
            payload['html'] = html_body

        headers = {'Authorization': f'Bearer {self.api_key}'}
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key

        client = self.client()
        try:
            with client:
                response = client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # The message may or may not have been accepted. Treated as
            # retryable, which is what the idempotency key is for.
            raise TransientSendError(f'{type(exc).__name__}: {exc}') from exc

        if response.status_code in PERMANENT_STATUSES:
            raise PermanentSendError(f'HTTP {response.status_code}: {summarize(response)}')
        if response.status_code >= 400:
            raise TransientSendError(f'HTTP {response.status_code}: {summarize(response)}')

        return SendResult(message_id=message_id(response))


def summarize(response):
    """A short, safe description of a failure. Never the whole body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    message = body.get('message') or body.get('error') or ''
    return str(message)[:200]


def message_id(response):
    try:
        return response.json().get('id')
    except ValueError:
        return None
