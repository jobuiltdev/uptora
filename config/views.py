import logging

from django.conf import settings
from django.db import connection
from django.http import Http404
from django.utils.crypto import constant_time_compare
from redis import Redis
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.status import HTTP_503_SERVICE_UNAVAILABLE
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class HealthView(APIView):
    """Liveness probe for the API."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # A liveness probe is polled on a fixed schedule; rate limiting it would
    # only ever produce false alarms.
    throttle_classes = []

    def get(self, request):
        return Response({'status': 'ok'})


class ReadinessView(APIView):
    """Protected, bounded dependency probe for operators and deployment health checks."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def get(self, request):
        supplied = request.headers.get('X-Uptora-Readiness-Key', '')
        if not settings.READINESS_TOKEN or not constant_time_compare(
            supplied,
            settings.READINESS_TOKEN,
        ):
            raise Http404

        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
            Redis.from_url(
                settings.CELERY_BROKER_URL,
                socket_connect_timeout=1,
                socket_timeout=1,
            ).ping()
        except Exception as exc:  # noqa: BLE001 - readiness must collapse dependency failures
            logger.warning('readiness dependency check failed: %s', type(exc).__name__)
            return Response({'status': 'not_ready'}, status=HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'status': 'ready'})
