from django.db import transaction
from rest_framework import serializers

from incidents.models import Incident, IncidentStatus
from monitors.models import CheckResult, FlowConfig, FlowField, Monitor, MonitorType
from websites.models import Website


class OwnedWebsiteField(serializers.PrimaryKeyRelatedField):
    """Website choices limited to those the requesting user owns.

    Scoping the queryset means attaching a monitor to somebody else's website
    fails as "object does not exist" rather than as a permission error, so the
    API never confirms that the other website is real.
    """

    def get_queryset(self):
        return Website.objects.filter(owner=self.context['request'].user)


def blank_to_none(value):
    """Treat an empty string as "not configured", so the check constraint agrees."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def require_selector(value, message):
    text = (value or '').strip()
    if not text:
        raise serializers.ValidationError(message)
    return text


class FlowFieldSerializer(serializers.ModelSerializer):
    """One control a flow fills.

    `value` is readable by the owner, which is what makes a configuration
    editable. It is never echoed into a check result or an incident. When login
    and payment flows arrive, this is the field that moves behind a secret
    store, and keeping it isolated here is what makes that a contained change.
    """

    class Meta:
        model = FlowField
        fields = ('id', 'selector', 'field_type', 'value', 'position')
        read_only_fields = ('id',)

    def validate_selector(self, value):
        return require_selector(value, 'A field selector is required.')


class FlowConfigSerializer(serializers.ModelSerializer):
    """Declarative flow configuration.

    Note what is absent: no URL, no action, no script. A flow runs against its
    website's own URL and submits the form found there, so configuration cannot
    turn Uptora into a request sender aimed wherever the configurer likes.
    """

    fields = FlowFieldSerializer(many=True)

    class Meta:
        model = FlowConfig
        fields = (
            'flow_kind',
            'submit_selector',
            'success_text',
            'success_selector',
            'success_url_contains',
            'fields',
        )

    def validate_submit_selector(self, value):
        return require_selector(value, 'A submit selector is required.')

    def validate(self, attrs):
        for name in ('success_text', 'success_selector', 'success_url_contains'):
            if name in attrs:
                attrs[name] = blank_to_none(attrs[name])

        if not any(
            attrs.get(name) for name in ('success_text', 'success_selector', 'success_url_contains')
        ):
            raise serializers.ValidationError(
                {
                    'success_text': [
                        'Configure at least one of success_text, success_selector or '
                        'success_url_contains. A submission with nothing checked '
                        'afterwards does not prove the form works.'
                    ]
                }
            )
        return attrs


def write_flow_config(monitor, config_data):
    """Replace a monitor's flow configuration wholesale.

    Fields are rewritten rather than merged: a partial update that left orphaned
    rows behind would silently keep filling controls the owner thought they had
    removed. Runs inside the caller's transaction, so a monitor is never left
    holding half a configuration.
    """
    fields_data = config_data.pop('fields', [])
    config, _ = FlowConfig.objects.update_or_create(monitor=monitor, defaults=config_data)

    config.fields.all().delete()
    FlowField.objects.bulk_create(
        FlowField(
            flow_config=config,
            selector=field['selector'],
            field_type=field.get('field_type', 'TEXT'),
            value=field.get('value', ''),
            position=field.get('position', index),
        )
        for index, field in enumerate(fields_data)
    )
    return config


class MonitorSerializer(serializers.ModelSerializer):
    website = OwnedWebsiteField()
    has_open_incident = serializers.SerializerMethodField()
    flow_config = FlowConfigSerializer(required=False, allow_null=True)

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
            'flow_config',
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

    def existing_config(self):
        if self.instance is None:
            return None
        return FlowConfig.objects.filter(monitor=self.instance).first()

    def validate(self, attrs):
        """A FLOW monitor is only meaningful with configuration attached.

        The mirror rule matters just as much: an HTTP or BROWSER monitor may not
        carry flow configuration, so there is no way to leave a form definition
        lying on a monitor that will never run it.
        """
        monitor_type = attrs.get(
            'monitor_type',
            getattr(self.instance, 'monitor_type', MonitorType.HTTP),
        )
        supplied = attrs.get('flow_config')

        if monitor_type == MonitorType.FLOW:
            if supplied is None and self.existing_config() is None:
                raise serializers.ValidationError(
                    {'flow_config': ['A flow monitor requires a flow_config.']}
                )
        elif supplied is not None:
            raise serializers.ValidationError(
                {
                    'flow_config': [
                        f'flow_config is only valid for FLOW monitors, not {monitor_type}.'
                    ]
                }
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        config_data = validated_data.pop('flow_config', None)
        monitor = super().create(validated_data)
        if config_data is not None:
            write_flow_config(monitor, config_data)
        return monitor

    @transaction.atomic
    def update(self, instance, validated_data):
        config_data = validated_data.pop('flow_config', None)
        monitor = super().update(instance, validated_data)
        if config_data is not None:
            write_flow_config(monitor, config_data)
        return monitor


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
