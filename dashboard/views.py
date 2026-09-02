from datetime import timedelta

from django.db.models import Exists, OuterRef, Prefetch, Subquery
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from incidents.models import Incident, IncidentStatus
from monitors.models import CheckResult, Monitor
from websites.models import Website

WEBSITE_LIMIT = 100
INCIDENT_LIMIT = 10
RECENT_CHECK_LIMIT = 20


def monitor_health(monitor):
    if not monitor.is_enabled:
        return 'DISABLED'
    if monitor.has_open_incident:
        return 'PROBLEM'
    if monitor.latest_success is None:
        return 'UNKNOWN'
    return 'HEALTHY' if monitor.latest_success else 'DEGRADED'


def website_health(website, monitors):
    enabled = [monitor for monitor in monitors if monitor.is_enabled]
    if not enabled:
        return 'PAUSED'
    states = {monitor_health(monitor) for monitor in enabled}
    for state in ('PROBLEM', 'DEGRADED', 'UNKNOWN'):
        if state in states:
            return state
    return 'HEALTHY'


def incident_data(incident):
    return {
        'id': incident.pk,
        'monitor': incident.monitor_id,
        'monitor_type': incident.monitor.monitor_type,
        'website': incident.monitor.website_id,
        'website_name': incident.monitor.website.name,
        'website_url': incident.monitor.website.url,
        'status': incident.status,
        'started_at': incident.started_at,
        'failure_type': incident.failure_type,
        'latest_error_message': incident.latest_error_message,
        'failure_count': incident.failure_count,
    }


class DashboardView(APIView):
    """A bounded, owner-scoped overview that never serializes flow configuration."""

    def get(self, request):
        owner = request.user
        latest = CheckResult.objects.filter(monitor=OuterRef('pk')).order_by('-checked_at', '-id')
        open_incidents = Incident.objects.filter(monitor=OuterRef('pk'), status=IncidentStatus.OPEN)
        dashboard_monitors = (
            Monitor.objects.filter(website__owner=owner)
            .annotate(
                latest_success=Subquery(latest.values('is_success')[:1]),
                latest_checked_at=Subquery(latest.values('checked_at')[:1]),
                has_open_incident=Exists(open_incidents),
            )
            .order_by('id')
        )
        websites = list(
            Website.objects.filter(owner=owner).prefetch_related(
                Prefetch('monitors', queryset=dashboard_monitors, to_attr='dashboard_monitors')
            )[:WEBSITE_LIMIT]
        )

        website_rows = []
        for website in websites:
            monitors = website.dashboard_monitors
            enabled = [monitor for monitor in monitors if monitor.is_enabled]
            checked = [m.latest_checked_at for m in monitors if m.latest_checked_at is not None]
            scheduled = [m.next_check_at for m in enabled if m.next_check_at is not None]
            website_rows.append(
                {
                    'id': website.pk,
                    'name': website.name,
                    'url': website.url,
                    'is_active': website.is_active,
                    'health': website_health(website, monitors),
                    'monitor_count': len(monitors),
                    'enabled_monitor_count': len(enabled),
                    'monitor_types': sorted({m.monitor_type for m in monitors}),
                    'active_incident_count': sum(m.has_open_incident for m in monitors),
                    'last_checked_at': max(checked) if checked else None,
                    'next_check_at': min(scheduled) if scheduled else None,
                }
            )

        active_incidents_qs = (
            Incident.objects.filter(monitor__website__owner=owner, status=IncidentStatus.OPEN)
            .select_related('monitor__website')
            .order_by('-started_at', '-id')[:INCIDENT_LIMIT]
        )
        recent_checks_qs = (
            CheckResult.objects.filter(monitor__website__owner=owner)
            .select_related('monitor__website')
            .order_by('-checked_at', '-id')[:RECENT_CHECK_LIMIT]
        )

        return Response(
            {
                'summary': {
                    'websites': Website.objects.filter(owner=owner).count(),
                    'enabled_monitors': dashboard_monitors.filter(is_enabled=True).count(),
                    'healthy_monitors': dashboard_monitors.filter(
                        is_enabled=True,
                        latest_success=True,
                        has_open_incident=False,
                    ).count(),
                    'active_incidents': Incident.objects.filter(
                        monitor__website__owner=owner, status=IncidentStatus.OPEN
                    ).count(),
                    'checks_24h': CheckResult.objects.filter(
                        monitor__website__owner=owner,
                        checked_at__gte=timezone.now() - timedelta(hours=24),
                    ).count(),
                },
                'websites': website_rows,
                'active_incidents': [incident_data(item) for item in active_incidents_qs],
                'recent_checks': [
                    {
                        'id': result.pk,
                        'monitor': result.monitor_id,
                        'monitor_type': result.monitor.monitor_type,
                        'website': result.monitor.website_id,
                        'website_name': result.monitor.website.name,
                        'is_success': result.is_success,
                        'status_code': result.status_code,
                        'response_time_ms': result.response_time_ms,
                        'error_type': result.error_type,
                        'error_message': result.error_message,
                        'checked_at': result.checked_at,
                        'has_evidence': bool(result.screenshot),
                    }
                    for result in recent_checks_qs
                ],
            }
        )
