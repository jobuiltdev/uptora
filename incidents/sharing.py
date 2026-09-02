from django.utils import timezone

from monitors.models import CheckResult, ErrorType

TIMELINE_LIMIT = 50

SAFE_FAILURE_SUMMARIES = {
    ErrorType.TIMEOUT: 'The check timed out before the target responded.',
    ErrorType.DNS_ERROR: 'The website address could not be resolved.',
    ErrorType.CONNECTION_ERROR: 'A connection to the website could not be established.',
    ErrorType.TLS_ERROR: 'The secure connection could not be verified.',
    ErrorType.HTTP_ERROR: 'The website returned an unsuccessful HTTP response.',
    ErrorType.TOO_MANY_REDIRECTS: 'The website redirected too many times.',
    ErrorType.BLOCKED_TARGET: 'The configured destination was not safe to check.',
    ErrorType.BROWSER_TIMEOUT: 'The rendered page did not finish within the allowed time.',
    ErrorType.NAVIGATION_ERROR: 'The browser could not finish opening the page.',
    ErrorType.EXPECTED_TEXT_MISSING: 'Expected page content was not found.',
    ErrorType.EXPECTED_SELECTOR_MISSING: 'An expected page element was not found.',
    ErrorType.BROWSER_ERROR: 'The rendered browser check did not complete successfully.',
    ErrorType.FLOW_CONFIGURATION_ERROR: 'The configured website flow could not be checked.',
    ErrorType.FLOW_FIELD_NOT_FOUND: 'A required form field was not found.',
    ErrorType.FLOW_FIELD_INTERACTION_ERROR: 'A required form field could not be completed.',
    ErrorType.FLOW_SUBMIT_NOT_FOUND: 'The form submission control was not found.',
    ErrorType.FLOW_SUBMIT_ERROR: 'The form could not be submitted.',
    ErrorType.FLOW_TIMEOUT: 'The website flow did not finish within the allowed time.',
    ErrorType.FLOW_SUCCESS_TEXT_MISSING: 'The expected confirmation content was not found.',
    ErrorType.FLOW_SUCCESS_SELECTOR_MISSING: 'The expected confirmation element was not found.',
    ErrorType.FLOW_SUCCESS_URL_MISMATCH: 'The flow did not reach the expected page.',
    ErrorType.FLOW_DESTINATION_NOT_ALLOWED: 'The form attempted to use an unapproved destination.',
    ErrorType.UNKNOWN_ERROR: 'The check did not complete successfully.',
}


def safe_failure_summary(error_type, status_code=None):
    summary = SAFE_FAILURE_SUMMARIES.get(error_type, 'The check did not complete successfully.')
    if error_type == ErrorType.HTTP_ERROR and status_code is not None:
        return f'{summary} Status code: {status_code}.'
    return summary


def incident_results(incident):
    queryset = CheckResult.objects.filter(
        monitor=incident.monitor,
        checked_at__gte=incident.started_at,
    )
    if incident.resolved_at is not None:
        queryset = queryset.filter(checked_at__lte=incident.resolved_at)
    queryset = queryset.order_by('checked_at', 'id')
    count = queryset.count()
    if count <= TIMELINE_LIMIT:
        return list(queryset)

    first_count = TIMELINE_LIMIT // 2
    last_count = TIMELINE_LIMIT - first_count
    first = list(queryset[:first_count])
    last = list(queryset.order_by('-checked_at', '-id')[:last_count])
    by_id = {result.pk: result for result in [*first, *reversed(last)]}
    return sorted(by_id.values(), key=lambda result: (result.checked_at, result.pk))


def choose_evidence_result(results):
    return next(
        (result for result in results if not result.is_success and bool(result.screenshot)),
        None,
    )


def build_share_snapshot(incident, results=None, evidence_result=None):
    results = results if results is not None else incident_results(incident)
    evidence_result = evidence_result or choose_evidence_result(results)
    timeline = []
    for index, result in enumerate(results):
        if result.is_success:
            label = (
                'Incident resolved'
                if incident.resolved_at is not None and result.checked_at == incident.resolved_at
                else 'Recovery check'
            )
            summary = 'The target completed this check successfully.'
            outcome = 'RECOVERY'
        else:
            label = 'Initial failure' if index == 0 else 'Confirmed failure'
            summary = safe_failure_summary(result.error_type, result.status_code)
            outcome = 'FAILURE'
        timeline.append(
            {
                'occurred_at': result.checked_at.isoformat(),
                'outcome': outcome,
                'label': label,
                'summary': summary,
                'status_code': result.status_code,
                'evidence_available': evidence_result is not None
                and result.pk == evidence_result.pk,
            }
        )

    return {
        'website_name': incident.monitor.website.name,
        'website_url': incident.monitor.website.url,
        'monitor_type': incident.monitor.monitor_type,
        'status': incident.status,
        'started_at': incident.started_at.isoformat(),
        'resolved_at': incident.resolved_at.isoformat() if incident.resolved_at else None,
        'failure_type': incident.failure_type,
        'failure_summary': safe_failure_summary(
            incident.failure_type,
            incident.initial_status_code,
        ),
        'latest_failure_summary': safe_failure_summary(
            incident.failure_type,
            incident.latest_status_code,
        ),
        'status_code': incident.latest_status_code or incident.initial_status_code,
        'failure_count': incident.failure_count,
        'recovery_count': incident.recovery_count,
        'timeline': timeline,
        'timeline_total_count': incident_results_count(incident),
        'captured_at': timezone.now().isoformat(),
    }


def merge_share_snapshot(existing, current):
    """Refresh current incident facts without discarding previously captured checks."""
    entries = [*existing.get('timeline', []), *current.get('timeline', [])]
    unique = {
        (
            entry.get('occurred_at'),
            entry.get('outcome'),
            entry.get('label'),
        ): entry
        for entry in entries
    }
    timeline = sorted(unique.values(), key=lambda entry: entry.get('occurred_at', ''))
    if len(timeline) > TIMELINE_LIMIT:
        first_count = TIMELINE_LIMIT // 2
        timeline = [*timeline[:first_count], *timeline[-(TIMELINE_LIMIT - first_count) :]]
    current['timeline'] = timeline
    current['timeline_total_count'] = max(
        existing.get('timeline_total_count', 0),
        current.get('timeline_total_count', 0),
        len(timeline),
    )
    return current


def incident_results_count(incident):
    queryset = CheckResult.objects.filter(
        monitor=incident.monitor,
        checked_at__gte=incident.started_at,
    )
    if incident.resolved_at is not None:
        queryset = queryset.filter(checked_at__lte=incident.resolved_at)
    return queryset.count()
