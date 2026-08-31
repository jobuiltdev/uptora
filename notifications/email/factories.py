"""Provider factories.

Settings names a callable rather than a class so the wiring -- API key, sender
address -- lives here instead of in settings, and a provider can be swapped
without either side knowing how the other is configured.
"""

from django.conf import settings

from notifications.email.console import ConsoleEmailProvider
from notifications.email.resend import ResendEmailProvider


def console_provider():
    return ConsoleEmailProvider()


def resend_provider():
    return ResendEmailProvider(
        api_key=settings.RESEND_API_KEY,
        from_address=settings.DEFAULT_FROM_EMAIL,
    )
