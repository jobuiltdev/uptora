"""Development provider.

The default when no RESEND_API_KEY is configured. It logs the subject and a
masked recipient and returns a message id marked `console:`, so it is obvious
from the record that nothing left the machine. Deliberately not a silent
no-op that reports success as though production delivery had happened.
"""

import logging
import uuid

from notifications.email.base import EmailProvider, SendResult

logger = logging.getLogger(__name__)


def mask(address):
    """Enough of an address to recognise, not enough to be a mailing list."""
    local, _, domain = (address or '').partition('@')
    if not domain:
        return '***'
    return f'{local[:2]}***@{domain}'


class ConsoleEmailProvider(EmailProvider):
    def send(self, to, subject, text_body, html_body=None, idempotency_key=None):
        logger.info('console email provider: would send %r to %s', subject, mask(to))
        return SendResult(message_id=f'console:{uuid.uuid4()}', detail='not actually sent')
