from rest_framework.routers import DefaultRouter

from websites.views import WebsiteViewSet

router = DefaultRouter()
router.register('websites', WebsiteViewSet, basename='website')

urlpatterns = router.urls
