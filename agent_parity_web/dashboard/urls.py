from django.urls import path

from dashboard import views, views_setup

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("devices/", views.device_list, name="device_list"),
    path("devices/<int:pk>/", views.device_detail, name="device_detail"),
    path("api/trend/<slug:slug>/", views.trend_data, name="trend_data"),
    path("setup/", views_setup.setup_overview, name="setup_overview"),
    path("setup/clients/new/", views_setup.client_form, name="client_create"),
    path("setup/clients/<slug:slug>/edit/", views_setup.client_form, name="client_edit"),
    path(
        "setup/vendors/<str:vendor>/new/",
        views_setup.vendor_account_create,
        name="vendor_account_create",
    ),
    path(
        "setup/vendors/<str:vendor>/<str:account>/",
        views_setup.vendor_credential_form,
        name="vendor_credential_form",
    ),
    path("setup/import/", views_setup.import_config_yaml, name="import_config_yaml"),
]
