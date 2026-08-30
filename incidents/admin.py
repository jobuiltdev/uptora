from django.contrib import admin

from incidents.models import Incident


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ('monitor', 'status', 'started_at', 'resolved_at', 'failure_type')
    list_filter = ('status', 'failure_type')
    date_hierarchy = 'started_at'
    search_fields = ('monitor__website__url', 'monitor__website__owner__email')
    # Incidents are produced by the engine, not edited by hand.
    readonly_fields = tuple(field.name for field in Incident._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
