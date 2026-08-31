from django.db.models import Exists, Max, OuterRef
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from incidents.models import Incident, IncidentStatus
from monitors.execution import execute_monitor
from monitors.models import Monitor
from monitors.serializers import CheckResultSerializer, MonitorSerializer


class MonitorDisabled(APIException):
    """A disabled monitor is a valid resource in the wrong state for running."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = 'This monitor is disabled and cannot be run.'
    default_code = 'monitor_disabled'


class CheckResultPagination(PageNumberPagination):
    """Applied only to the results action.

    Declared here rather than as a project-wide default so the existing list
    endpoints keep returning plain arrays.
    """

    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class MonitorViewSet(viewsets.ModelViewSet):
    """CRUD for monitors on the requesting user's own websites.

    Ownership travels through the website, so the queryset filter is the only
    place it has to be enforced; a foreign monitor is simply not found.
    """

    serializer_class = MonitorSerializer

    def get_queryset(self):
        open_incidents = Incident.objects.filter(monitor=OuterRef('pk'), status=IncidentStatus.OPEN)
        return (
            Monitor.objects.filter(website__owner=self.request.user)
            .select_related('website')
            .annotate(
                has_open_incident=Exists(open_incidents),
                last_check_at=Max('results__checked_at'),
            )
            .prefetch_related('flow_config__fields')
        )

    def get_throttles(self):
        # Running a monitor makes an outbound request on demand, so it gets a
        # much tighter limit than ordinary reads and writes.
        if self.action == 'run':
            self.throttle_scope = 'monitor_run'
            return [ScopedRateThrottle()]
        return super().get_throttles()

    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """Execute the monitor synchronously and return the new CheckResult.

        Development and validation scaffolding. Celery will call
        execute_monitor on a schedule; this endpoint exists so the engine can be
        exercised by hand until then.
        """
        monitor = self.get_object()
        if not monitor.is_enabled:
            raise MonitorDisabled()

        result = execute_monitor(monitor)
        serializer = CheckResultSerializer(result)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def results(self, request, pk=None):
        """Recent check history for this monitor, newest first and paginated."""
        monitor = self.get_object()
        paginator = CheckResultPagination()
        page = paginator.paginate_queryset(monitor.results.all(), request, view=self)
        serializer = CheckResultSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)
