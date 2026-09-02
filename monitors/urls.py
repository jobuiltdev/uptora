from django.urls import path
from rest_framework.routers import DefaultRouter

from monitors.views import CheckResultEvidenceView, MonitorViewSet

router = DefaultRouter()
router.register('monitors', MonitorViewSet, basename='monitor')

urlpatterns = [
    path(
        'check-results/<int:pk>/evidence/',
        CheckResultEvidenceView.as_view(),
        name='check-result-evidence',
    ),
    *router.urls,
]
