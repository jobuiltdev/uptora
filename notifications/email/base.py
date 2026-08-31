"""The email provider contract.

Business logic talks to this, never to a vendor SDK. Swapping Resend for
anything else is then a settings change and one new module, and the delivery
state machine does not have to learn a second vocabulary of errors.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SendResult:
    """What the provider said when it accepted the message."""

    message_id: str | None = None
    detail: str | None = None


class SendError(Exception):
    """Base for anything that stopped a message going out."""


class TransientSendError(SendError):
    """Worth trying again: a timeout, a 5xx, a rate limit."""


class PermanentSendError(SendError):
    """Not worth trying again: a malformed address, a rejected sender.

    Retrying these only burns attempts and delays the FAILED state that tells
    an operator something is actually wrong with the configuration.
    """


class EmailProvider:
    """Send one message. Implementations raise the two errors above."""

    def send(self, to, subject, text_body, html_body=None, idempotency_key=None):
        raise NotImplementedError
