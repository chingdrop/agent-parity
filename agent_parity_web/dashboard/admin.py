from django.contrib import admin

from dashboard.models import (
    Client,
    CorrelationRun,
    CoverageSnapshot,
    Device,
    VendorCredential,
)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "is_active",
        "enabled_vendors",
        "ad_target_devices",
        "sync_interval_hours",
    )
    prepopulated_fields = {"slug": ("name",)}


@admin.register(VendorCredential)
class VendorCredentialAdmin(admin.ModelAdmin):
    # "credentials" is excluded from list_display/search (never in the
    # changelist) and made read-only in the change form: EncryptedJSONField
    # has no custom form widget, so the stock admin Textarea would round-trip
    # the field's str() through get_prep_value on save and silently corrupt
    # it into "{'api_url': ...}" instead of decryptable JSON. Real editing is
    # the setup page's per-vendor form (dashboard/forms.py), not admin.
    list_display = ("vendor", "client")
    list_filter = ("vendor", "client")
    readonly_fields = ("credentials",)


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
    list_display = ("device", "run", "status", "vendor", "platform", "machine_type", "agent_last_seen")
    list_filter = ("status", "vendor", "platform", "machine_type", "run__client")
    search_fields = ("device__join_key",)
    list_select_related = ("device", "run")
