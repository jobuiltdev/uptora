from rest_framework import serializers

from monitors.models import CheckResult, Monitor
from websites.models import Website


class OwnedWebsiteField(serializers.PrimaryKeyRelatedField):
    """Website choices limited to those the requesting user owns.

    Scoping the queryset means attaching a monitor to somebody else's website
    fails as "object does not exist" rather than as a permission error, so the
    API never confirms that the other website is real.
    """

    def get_queryset(self):
        return Website.objects.filter(owner=self.context['request'].user)


class MonitorSerializer(serializers.ModelSerializer):
    website = OwnedWebsiteField()

    class Meta:
        model = Monitor
        fields = (
            'id',
            'website',
            'monitor_type',
            'is_enabled',
            'interval_seconds',
            'timeout_seconds',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class CheckResultSerializer(serializers.ModelSerializer):
    """Read-only: results are an append-only history."""

    class Meta:
        model = CheckResult
        fields = (
            'id',
            'monitor',
            'checked_at',
            'is_success',
            'status_code',
            'response_time_ms',
            'error_type',
            'error_message',
            'ssl_expires_at',
            'ssl_days_remaining',
        )
        read_only_fields = fields
