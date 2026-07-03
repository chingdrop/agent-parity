from django.contrib import admin

from dashboard.models import Client, CorrelationRun, CoverageSnapshot, Device


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "enabled_vendors")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("join_key", "hostname", "client", "os", "first_seen", "last_seen")
    list_filter = ("client",)
    search_fields = ("join_key", "hostname")


@admin.register(CorrelationRun)
class CorrelationRunAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "started_at", "status", "stale_days", "vendor_status")
    list_filter = ("client", "status")
    date_hierarchy = "started_at"


@admin.register(CoverageSnapshot)
class CoverageSnapshotAdmin(admin.ModelAdmin):
    list_display = ("device", "run", "status", "vendor", "agent_last_seen")
    list_filter = ("status", "vendor", "run__client")
    search_fields = ("device__join_key",)
    list_select_related = ("device", "run")
