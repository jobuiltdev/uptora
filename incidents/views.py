from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

from incidents.models import Incident, IncidentStatus
from incidents.serializers import IncidentSerializer


class IncidentPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    """Incident history for the requesting user's own monitors.

    Read-only by design: incidents are produced by the monitoring engine, never
    by a client. Ownership is enforced by scoping the queryset through the
    monitor's website, so somebody else's incident is simply not found.
    """

    serializer_class = IncidentSerializer
    pagination_class = IncidentPagination

    def get_queryset(self):
        queryset = Incident.objects.filter(
            monitor__website__owner=self.request.user
        ).select_related('monitor__website')
        return self.apply_filters(queryset)

    def apply_filters(self, queryset):
        """Two exact-match filters. Not enough surface to justify django-filter."""
        params = self.request.query_params

        status = params.get('status')
        if status:
            value = status.upper()
            if value not in IncidentStatus.values:
                raise ValidationError(
                    {'status': [f'Must be one of: {", ".join(IncidentStatus.values)}.']}
                )
            queryset = queryset.filter(status=value)

        monitor = params.get('monitor')
        if monitor:
            try:
                monitor_id = int(monitor)
            except (TypeError, ValueError) as exc:
                raise ValidationError({'monitor': ['Must be an integer id.']}) from exc
            queryset = queryset.filter(monitor_id=monitor_id)

        return queryset
