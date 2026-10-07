from django.urls import include, path

urlpatterns = [
    path("", include("filler.urls")),
]
