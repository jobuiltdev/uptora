from django.urls import path
from rest_framework.routers import DefaultRouter

from incidents.views import (
    IncidentViewSet,
    PublicIncidentEvidenceView,
    PublicIncidentShareView,
)

router = DefaultRouter()
router.register('incidents', IncidentViewSet, basename='incident')

urlpatterns = [
    path(
        'public/incident-shares/<str:token>/',
        PublicIncidentShareView.as_view(),
        name='public-incident-share',
    ),
    path(
        'public/incident-shares/<str:token>/evidence/',
        PublicIncidentEvidenceView.as_view(),
        name='public-incident-share-evidence',
    ),
    *router.urls,
]
