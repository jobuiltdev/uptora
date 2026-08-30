from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness probe for the API."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # A liveness probe is polled on a fixed schedule; rate limiting it would
    # only ever produce false alarms.
    throttle_classes = []

    def get(self, request):
        return Response({'status': 'ok'})
