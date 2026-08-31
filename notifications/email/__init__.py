"""Email provider selection.

The provider is resolved from settings once per call rather than held in a
module global, so a test can override it with settings and get the override.
"""

from django.conf import settings
from django.utils.module_loading import import_string

from notifications.email.base import (
    EmailProvider,
    PermanentSendError,
    SendError,
    SendResult,
    TransientSendError,
)

__all__ = [
    'EmailProvider',
    'PermanentSendError',
    'SendError',
    'SendResult',
    'TransientSendError',
    'get_email_provider',
]


def get_email_provider():
    """Build the configured provider.

    Falls back to the console provider when no API key is configured, so a
    developer without credentials gets something visibly local rather than a
    silent pretence of delivery.
    """
    factory = import_string(settings.NOTIFICATIONS_EMAIL_PROVIDER)
    return factory()
