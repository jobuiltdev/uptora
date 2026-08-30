"""Shared result type for check implementations.

A check returns one of these and touches no database and no request objects, so
the same function is usable from a view, a management command or a Celery task.
"""

from dataclasses import asdict, dataclass

ERROR_MESSAGE_LIMIT = 300


def summarize(value):
    """Condense an exception or message into one short line.

    Results are queried and displayed constantly; storing stack traces here
    would bloat the table and tell an operator nothing useful.
    """
    text = str(value).strip().splitlines()
    message = text[0].strip() if text else ''
    if not message:
        message = type(value).__name__ if isinstance(value, BaseException) else 'Unknown error'
    if len(message) > ERROR_MESSAGE_LIMIT:
        message = message[: ERROR_MESSAGE_LIMIT - 1].rstrip() + '…'
    return message


# Not a column. The screenshot is a file, saved through Django storage by the
# persistence layer rather than written into the row.
NON_COLUMN_FIELDS = frozenset({'screenshot'})


@dataclass(frozen=True)
class CheckOutcome:
    """What a single check observed, before it is persisted."""

    is_success: bool
    status_code: int | None = None
    response_time_ms: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    final_url: str | None = None
    ssl_expires_at: object | None = None
    ssl_days_remaining: int | None = None
    # Evidence bytes for a failed browser check, or None. Always None on success.
    screenshot: bytes | None = None

    @classmethod
    def failure(cls, error_type, message, **extra):
        return cls(
            is_success=False,
            error_type=error_type,
            error_message=summarize(message),
            **extra,
        )

    def as_result_fields(self):
        """The values that map directly onto CheckResult columns."""
        return {
            name: value for name, value in asdict(self).items() if name not in NON_COLUMN_FIELDS
        }
