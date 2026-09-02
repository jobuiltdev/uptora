from django.db import transaction
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from incidents.models import Incident, IncidentShare, IncidentStatus
from incidents.serializers import (
    IncidentSerializer,
    IncidentShareCreateSerializer,
    IncidentShareSerializer,
)
from incidents.sharing import (
    build_share_snapshot,
    choose_evidence_result,
    incident_results,
    merge_share_snapshot,
)


class IncidentPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    """Incident history and sharing controls scoped through website ownership."""

    serializer_class = IncidentSerializer
    pagination_class = IncidentPagination

    def get_queryset(self):
        queryset = Incident.objects.filter(
            monitor__website__owner=self.request.user
        ).select_related('monitor__website')
        return self.apply_filters(queryset)

    def apply_filters(self, queryset):
        params = self.request.query_params
        status_value = params.get('status')
        if status_value:
            value = status_value.upper()
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

    def active_share(self, incident):
        now = timezone.now()
        unrevoked = incident.shares.filter(revoked_at__isnull=True)
        return (
            unrevoked.filter(expires_at__isnull=True).first()
            or unrevoked.filter(expires_at__gt=now).first()
        )

    def create_share(self, incident, values):
        now = timezone.now()
        IncidentShare.objects.filter(
            incident=incident,
            revoked_at__isnull=True,
            expires_at__lte=now,
        ).update(revoked_at=now)
        if IncidentShare.objects.filter(
            incident=incident,
            revoked_at__isnull=True,
        ).exists():
            return None
        results = incident_results(incident)
        evidence_result = choose_evidence_result(results)
        return IncidentShare.objects.create(
            incident=incident,
            expires_at=values.get('expires_at'),
            include_evidence=values.get('include_evidence', True),
            snapshot=build_share_snapshot(
                incident,
                results=results,
                evidence_result=evidence_result,
            ),
            snapshot_updated_at=now,
            evidence_result=evidence_result,
        )

    @action(detail=True, methods=['get', 'post', 'delete'], url_path='share')
    def share(self, request, pk=None):
        incident = self.get_object()
        active = self.active_share(incident)
        if request.method == 'GET':
            if active is None:
                raise Http404
            return Response(IncidentShareSerializer(active).data)
        if request.method == 'DELETE':
            if active is None:
                raise Http404
            active.revoked_at = timezone.now()
            active.save(update_fields=['revoked_at'])
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = IncidentShareCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            incident = Incident.objects.select_for_update().get(pk=incident.pk)
            share = self.create_share(incident, serializer.validated_data)
        if share is None:
            return Response(
                {'detail': 'An active share already exists for this incident.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(IncidentShareSerializer(share).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='share/regenerate')
    def regenerate_share(self, request, pk=None):
        incident = self.get_object()
        serializer = IncidentShareCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        now = timezone.now()
        with transaction.atomic():
            incident = Incident.objects.select_for_update().get(pk=incident.pk)
            IncidentShare.objects.filter(
                incident=incident,
                revoked_at__isnull=True,
            ).update(revoked_at=now)
            share = self.create_share(incident, serializer.validated_data)
        return Response(IncidentShareSerializer(share).data, status=status.HTTP_201_CREATED)


def get_public_share(token):
    try:
        share = IncidentShare.objects.select_related(
            'incident__monitor__website',
            'evidence_result__monitor',
        ).get(token=token)
    except IncidentShare.DoesNotExist as exc:
        raise Http404 from exc
    if not share.is_available():
        raise Http404
    return share


def evidence_is_in_share_context(share):
    result = share.evidence_result
    incident = share.incident
    if result is None or result.monitor_id != incident.monitor_id or not result.screenshot:
        return False
    if result.checked_at < incident.started_at:
        return False
    return incident.resolved_at is None or result.checked_at <= incident.resolved_at


class PublicIncidentShareView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token):
        share = get_public_share(token)
        snapshot = dict(share.snapshot)
        incident = share.incident
        if incident.updated_at > share.snapshot_updated_at:
            results = incident_results(incident)
            evidence_result = choose_evidence_result(results)
            snapshot = merge_share_snapshot(
                snapshot,
                build_share_snapshot(
                    incident,
                    results=results,
                    evidence_result=evidence_result,
                ),
            )
            share.snapshot = snapshot
            share.snapshot_updated_at = timezone.now()
            share.evidence_result = evidence_result
            share.save(update_fields=['snapshot', 'snapshot_updated_at', 'evidence_result'])

        end = incident.resolved_at or timezone.now()
        snapshot['duration_seconds'] = max(
            0,
            int((end - incident.started_at).total_seconds()),
        )
        snapshot['evidence_available'] = share.include_evidence and evidence_is_in_share_context(
            share
        )
        return Response(snapshot)


class PublicIncidentEvidenceView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token):
        share = get_public_share(token)
        if not share.include_evidence or not evidence_is_in_share_context(share):
            raise Http404
        try:
            stream = share.evidence_result.screenshot.open('rb')
        except (FileNotFoundError, OSError) as exc:
            raise Http404 from exc
        return FileResponse(
            stream,
            content_type='image/png',
            as_attachment=False,
            filename='uptora-shared-incident-evidence.png',
        )
