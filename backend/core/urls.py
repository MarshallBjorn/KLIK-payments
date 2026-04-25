"""
URL configuration for core project.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('common.urls')),
    # path('api/v1/', include('api.urls')),  # TBD: gdy zaczniemy implementację API
]
