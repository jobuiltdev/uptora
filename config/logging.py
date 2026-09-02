import logging
import re


class RedactCapabilitiesFilter(logging.Filter):
    """Keep capability and bearer tokens out of application-managed logs."""

    patterns = (
        re.compile(r'(/(?:api/public/incident-shares|share/incidents)/)[A-Za-z0-9_-]+'),
        re.compile(r'(Bearer\s+)[A-Za-z0-9._-]+', re.IGNORECASE),
    )

    @classmethod
    def redact(cls, value):
        if not isinstance(value, str):
            return value
        for pattern in cls.patterns:
            value = pattern.sub(r'\1[REDACTED]', value)
        return value

    def filter(self, record):
        record.msg = self.redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self.redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self.redact(value) for key, value in record.args.items()}
        return True
