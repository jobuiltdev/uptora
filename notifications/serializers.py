from rest_framework import serializers

from notifications.models import NotificationPreference


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    """The current user's alert settings.

    The user is not a field. It comes from the request, so there is no id to
    supply and no way to read or write somebody else's preferences.
    """

    class Meta:
        model = NotificationPreference
        fields = ('email_enabled', 'alert_email', 'created_at', 'updated_at')
        read_only_fields = ('created_at', 'updated_at')

    def validate_alert_email(self, value):
        """Blank means "use the account address", not an empty recipient."""
        if value is None or not value.strip():
            return None
        return value.strip()
