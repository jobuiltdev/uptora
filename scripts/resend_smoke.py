"""Manual Resend smoke test. Never run by pytest.

Sends exactly one clearly-marked test email through the real provider, so a
developer can confirm credentials and deliverability without waiting for a real
outage and without any chance of a customer receiving a spurious alert.

    python scripts_resend_smoke.py you@example.com

Requires RESEND_API_KEY and DEFAULT_FROM_EMAIL. The recipient must be supplied
on the command line: there is deliberately no default and no lookup of a real
account address.
"""

import os
import sys

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.conf import settings  # noqa: E402

from notifications.email.factories import resend_provider  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    recipient = sys.argv[1]
    if not settings.RESEND_API_KEY:
        print('RESEND_API_KEY is not set; nothing was sent.')
        return 1

    result = resend_provider().send(
        to=recipient,
        subject='[Uptora] Test email - please ignore',
        text_body=(
            'This is a manual Uptora smoke test.\n\n'
            'It is not an alert about any website and no monitor produced it.\n'
        ),
        html_body='<p>This is a manual Uptora smoke test. Not an alert.</p>',
        idempotency_key=f'smoke-{recipient}',
    )
    print(f'sent, provider message id: {result.message_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
