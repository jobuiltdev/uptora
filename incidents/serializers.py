from rest_framework import serializers

from incidents.models import Incident


class IncidentSerializer(serializers.ModelSerializer):
    """Read-only view of an incident.

    Website details are flattened rather than nested: a dashboard needs to say
    what broke without pulling a whole website object per row.
    """

    website = serializers.IntegerField(source='monitor.website_id', read_only=True)
    website_name = serializers.CharField(source='monitor.website.name', read_only=True)
    website_url = serializers.CharField(source='monitor.website.url', read_only=True)
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Incident
        fields = (
            'id',
            'monitor',
            'website',
            'website_name',
            'website_url',
            'status',
            'started_at',
            'resolved_at',
            'duration_seconds',
            'failure_type',
            'initial_error_message',
            'latest_error_message',
            'initial_status_code',
            'latest_status_code',
            'failure_count',
            'recovery_count',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def get_duration_seconds(self, incident):
        """How long the incident lasted, or None while it is still open."""
        if incident.resolved_at is None:
            return None
        return int((incident.resolved_at - incident.started_at).total_seconds())
