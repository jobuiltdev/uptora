"""In-memory provider for tests.

Records what would have been sent and can be told to fail, so delivery
behaviour is testable without a network or an API key.
"""

import itertools

from notifications.email.base import EmailProvider, SendResult


class FakeEmailProvider(EmailProvider):
    def __init__(self, error=None):
        self.sent = []
        self.error = error
        self._ids = itertools.count(1)

    def send(self, to, subject, text_body, html_body=None, idempotency_key=None):
        self.sent.append(
            {
                'to': to,
                'subject': subject,
                'text_body': text_body,
                'html_body': html_body,
                'idempotency_key': idempotency_key,
            }
        )
        if self.error is not None:
            raise self.error
        return SendResult(message_id=f'fake-{next(self._ids)}')

    @property
    def calls(self):
        return len(self.sent)

    @property
    def last(self):
        return self.sent[-1] if self.sent else None
