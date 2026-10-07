from django.urls import path

from . import views

urlpatterns = [
    path("", views.upload, name="upload"),
    path("settings/", views.settings_view, name="settings"),
    path("jobs/<int:pk>/", views.job_detail, name="job"),
    path("jobs/<int:pk>/file/", views.job_file, name="job_file"),
    path("jobs/<int:pk>/delete/", views.job_delete, name="job_delete"),
    path("jobs/<int:pk>/status.json", views.job_status, name="job_status"),
    path("rows/<int:pk>/", views.row_review, name="row"),
    path("rows/<int:pk>/screenshot.jpg", views.row_shot, name="row_shot"),
    path("rows/<int:pk>/status.json", views.row_status, name="row_status"),
]
