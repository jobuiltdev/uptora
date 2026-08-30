from rest_framework import serializers

from incidents.models import Incident, IncidentStatus
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
    has_open_incident = serializers.SerializerMethodField()

    class Meta:
        model = Monitor
        fields = (
            'id',
            'website',
            'monitor_type',
            'is_enabled',
            'interval_seconds',
            'timeout_seconds',
            'expected_text',
            'expected_selector',
            'has_open_incident',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_has_open_incident(self, monitor):
        """Whether this monitor is currently down.

        A single flag rather than embedded incident history: a list of monitors
        needs to show a status dot, and anything more belongs on /api/incidents/.

        The list and detail views annotate this so a page of monitors costs one
        query. Create and update responses serialize a freshly saved instance
        that carries no annotation, so those fall back to a single exists().
        """
        annotated = getattr(monitor, 'has_open_incident', None)
        if annotated is not None:
            return annotated
        return Incident.objects.filter(monitor=monitor, status=IncidentStatus.OPEN).exists()


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
            'final_url',
            'screenshot',
            'ssl_expires_at',
            'ssl_days_remaining',
        )
        read_only_fields = fields
