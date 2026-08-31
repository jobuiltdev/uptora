from django.contrib import admin

from monitors.models import CheckResult, FlowConfig, FlowField, Monitor, MonitorRun


class FlowFieldInline(admin.TabularInline):
    model = FlowField
    extra = 0


@admin.register(FlowConfig)
class FlowConfigAdmin(admin.ModelAdmin):
    list_display = ('monitor', 'flow_kind', 'submit_selector')
    list_filter = ('flow_kind',)
    inlines = [FlowFieldInline]


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


@admin.register(MonitorRun)
class MonitorRunAdmin(admin.ModelAdmin):
    """Operational view of the scheduler. Read-only: runs are produced by
    workers, never edited by hand, and the lease token is machinery rather than
    information."""

    list_display = (
        'id',
        'monitor',
        'status',
        'scheduled_for',
        'started_at',
        'finished_at',
        'check_result',
        'attempt_count',
    )
    list_filter = ('status',)
    date_hierarchy = 'scheduled_for'
    search_fields = ('id', 'monitor__website__url')
    readonly_fields = tuple(
        field.name for field in MonitorRun._meta.fields if field.name != 'claim_token'
    )
    exclude = ('claim_token',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
