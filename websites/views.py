from rest_framework import viewsets

from websites.models import Website
from websites.serializers import WebsiteSerializer


class WebsiteViewSet(viewsets.ModelViewSet):
    """CRUD for the requesting user's own websites.

    Ownership is enforced by scoping the queryset, so another user's website is
    simply not found rather than forbidden.
    """

    serializer_class = WebsiteSerializer

    def get_queryset(self):
        return Website.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
