from rest_framework import generics

from notifications.models import NotificationPreference
from notifications.serializers import NotificationPreferenceSerializer


class NotificationPreferenceView(generics.RetrieveUpdateAPIView):
    """The requesting user's notification settings.

    A singleton per user with no id in the URL, so there is no object to
    enumerate and no possibility of reading another account's settings.

    The row is created on first access rather than eagerly for every signup: a
    user who never opens this endpoint is still notified, because a missing row
    means the defaults.
    """

    serializer_class = NotificationPreferenceSerializer

    def get_object(self):
        preference, _ = NotificationPreference.objects.get_or_create(user=self.request.user)
        return preference
