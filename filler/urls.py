from django.urls import path

from . import views

urlpatterns = [
    path("", views.upload, name="upload"),
    path("settings/", views.settings_view, name="settings"),
    path("jobs/<int:pk>/", views.job_detail, name="job"),
    path("rows/<int:pk>/", views.row_review, name="row"),
]
