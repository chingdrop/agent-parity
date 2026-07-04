from django.urls import path

from dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("devices/", views.device_list, name="device_list"),
    path("devices/<int:pk>/", views.device_detail, name="device_detail"),
    path("api/trend/<slug:slug>/", views.trend_data, name="trend_data"),
]
