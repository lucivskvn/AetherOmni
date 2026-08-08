from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "AetherOmni Administrative Portal"
admin.site.site_title = "AetherOmni Admin"
admin.site.index_title = "System Database & Configuration Administration"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("extractor.urls")),  # Root URL maps to extractor app dashboard
]

# Serve media files locally during development (when GCS bucket is not configured)
if settings.GS_BUCKET_NAME is None:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
