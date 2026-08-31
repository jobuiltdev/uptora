from django.contrib import admin

from notifications.models import (
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'email_enabled', 'alert_email', 'updated_at')
    list_filter = ('email_enabled',)
    search_fields = ('user__email', 'alert_email')


@admin.register(NotificationEvent)
class NotificationEventAdmin(admin.ModelAdmin):
    """Read-only: events are produced by the incident engine, never by hand."""

    # The snapshot columns stay useful after an incident is withdrawn, which
    # is the whole point of holding them here.
    list_display = (
        'id',
        'event_type',
        'website_name',
        'website_url',
        'monitor_ref',
        'incident',
        'occurred_at',
    )
    list_filter = ('event_type', 'monitor_type')
    date_hierarchy = 'occurred_at'
    search_fields = ('id', 'website_url', 'website_name')
    readonly_fields = tuple(field.name for field in NotificationEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    """Operational view of what was sent and what failed.

    The lease token is machinery rather than information and is hidden.
    """

    list_display = (
        'id',
        'event',
        'channel',
        'recipient',
        'status',
        'attempt_count',
        'next_attempt_at',
        'sent_at',
    )
    list_filter = ('status', 'channel')
    date_hierarchy = 'created_at'
    search_fields = ('id', 'recipient', 'provider_message_id')
    readonly_fields = tuple(
        field.name for field in NotificationDelivery._meta.fields if field.name != 'claim_token'
    )
    exclude = ('claim_token',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
