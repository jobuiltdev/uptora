from django.urls import path

from notifications.views import NotificationPreferenceView

urlpatterns = [
    path(
        'notifications/preferences/',
        NotificationPreferenceView.as_view(),
        name='notification-preferences',
    ),
]
