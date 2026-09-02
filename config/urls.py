"""URL configuration for the Uptora project."""

from django.contrib import admin
from django.urls import include, path

from config.views import HealthView, ReadinessView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', HealthView.as_view(), name='health'),
    path('api/readiness/', ReadinessView.as_view(), name='readiness'),
    path('api/auth/', include('accounts.urls')),
    path('api/', include('websites.urls')),
    path('api/', include('monitors.urls')),
    path('api/', include('incidents.urls')),
    path('api/', include('notifications.urls')),
    path('api/', include('dashboard.urls')),
]
