from django.contrib import admin

from monitors.models import CheckResult, Monitor


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ('website', 'monitor_type', 'is_enabled', 'interval_seconds', 'timeout_seconds')
    list_filter = ('monitor_type', 'is_enabled')
    search_fields = ('website__url', 'website__name', 'website__owner__email')


@admin.register(CheckResult)
class CheckResultAdmin(admin.ModelAdmin):
    list_display = ('monitor', 'checked_at', 'is_success', 'status_code', 'error_type')
    list_filter = ('is_success', 'error_type')
    date_hierarchy = 'checked_at'
    # Results are an append-only history.
    readonly_fields = tuple(field.name for field in CheckResult._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
