"""Relational schema for correlation results.

A Client owns Devices; every pipeline execution is a CorrelationRun; each run
produces one CoverageSnapshot per device/vendor observation, FK'd to both —
so per-device history and per-run aggregates are both plain queries.
"""

from django.db import models
from django.db.models import Q
from django.utils import timezone

from agent_parity.connectors import CONNECTOR_CLASSES
from agent_parity.models import CoverageStatus as PipelineStatus
from agent_parity.models import OSLifecycleStatus as PipelineOSLifecycleStatus
from dashboard.fields import EncryptedJSONField

#: Vendor names known to the pipeline (agent_parity.connectors.CONNECTOR_CLASSES
#: is the single source of truth so this can't drift from what connectors
#: actually exist).
VENDOR_CHOICES = [(name, name) for name in sorted(CONNECTOR_CLASSES)]


class CoverageStatus(models.TextChoices):
    """ORM mirror of the pipeline's CoverageStatus enum (same values)."""

    COVERED = PipelineStatus.COVERED.value, "Covered"
    MISSING_AGENT = PipelineStatus.MISSING_AGENT.value, "Missing agent"
    ORPHANED_AGENT = PipelineStatus.ORPHANED_AGENT.value, "Orphaned agent"
    STALE_COVERAGE = PipelineStatus.STALE_COVERAGE.value, "Stale coverage"


class OSLifecycleStatus(models.TextChoices):
    """ORM mirror of the pipeline's OSLifecycleStatus enum (same values)."""

    UNKNOWN = PipelineOSLifecycleStatus.UNKNOWN.value, "Unknown"
    SUPPORTED = PipelineOSLifecycleStatus.SUPPORTED.value, "Supported"
    EOL_SOON = PipelineOSLifecycleStatus.EOL_SOON.value, "EOL soon"
    END_OF_LIFE = PipelineOSLifecycleStatus.END_OF_LIFE.value, "End of life"


class Client(models.Model):
    """One managed organization/environment."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(default=True)
    # Which vendors this client uses; drives which VendorCredential rows are
    # looked up and which fan-out tasks get dispatched for it.
    enabled_vendors = models.JSONField(default=list, blank=True)
    # Domain-joined endpoints the AD export script is pushed to — one per AD
    # domain (see ClientConfig.ad_target_devices in agent_parity/config.py);
    # a single-domain client just has one entry.
    ad_target_devices = models.JSONField(default=list, blank=True)
    sync_interval_hours = models.PositiveIntegerField(default=24)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class VendorCredential(models.Model):
    """A vendor's API credentials, encrypted at rest.

    ``client`` is null for global-scope vendors (SentinelOne, BitDefender —
    one credential set for the whole organization) and set for per-client
    vendors (Carbon Black — a distinct credential set per client). Which
    vendors are global vs per-client is a fixed business fact, not something
    this model or a setup form decides — see
    ``agent_parity.config.VENDOR_SCOPE``.
    """

    client = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.CASCADE, related_name="vendor_credentials"
    )
    vendor = models.CharField(max_length=32, choices=VENDOR_CHOICES)
    # Vendor-specific shape (e.g. {"api_url", "api_token"} for SentinelOne vs
    # {"api_url", "api_id", "api_key", "org_key"} for Carbon Black) — a
    # single encrypted blob rather than per-vendor columns, same as
    # VendorConfig.credentials/ClientConfig.vendors in agent_parity/config.py.
    credentials = EncryptedJSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vendor"], condition=Q(client__isnull=True), name="uniq_global_vendor_credential"
            ),
            models.UniqueConstraint(
                fields=["client", "vendor"],
                condition=Q(client__isnull=False),
                name="uniq_per_client_vendor_credential",
            ),
        ]

    def __str__(self):
        return f"{self.client.slug if self.client else 'global'}/{self.vendor}"


class Device(models.Model):
    """A device identity, keyed by the normalized hostname join key."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="devices")
    join_key = models.CharField(max_length=255, db_index=True)
    hostname = models.CharField(max_length=255)
    os = models.CharField(max_length=255, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["client", "join_key"], name="uniq_device_per_client")
        ]
        ordering = ["join_key"]

    def __str__(self):
        return f"{self.client.slug}/{self.join_key}"


class CorrelationRun(models.Model):
    """One pipeline execution for one client."""

    class RunStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETE = "complete", "Complete"
        PARTIAL = "partial", "Partial (some vendors failed)"
        FAILED = "failed", "Failed"

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="runs")
    # default (not auto_now_add) so seeded demo history can be backdated.
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=RunStatus.choices, default=RunStatus.PENDING)
    # Config snapshot: what threshold this run was classified with.
    stale_days = models.PositiveIntegerField(default=14)
    # Per-source outcome, e.g. {"ad": "ok", "sentinelone": "ok",
    # "carbonblack": "error: ..."} — how partial runs stay honest.
    vendor_status = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        get_latest_by = "started_at"

    def __str__(self):
        return f"{self.client.slug} run {self.pk} ({self.started_at:%Y-%m-%d %H:%M})"


class CoverageSnapshot(models.Model):
    """One device/vendor observation within one run."""

    run = models.ForeignKey(CorrelationRun, on_delete=models.CASCADE, related_name="snapshots")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="snapshots")
    status = models.CharField(max_length=32, choices=CoverageStatus.choices, db_index=True)
    # Empty for missing_agent rows (no vendor observed the device).
    vendor = models.CharField(max_length=32, blank=True)
    match_method = models.CharField(max_length=32, blank=True)
    # The agent's check-in time as of this run (drives staleness).
    agent_last_seen = models.DateTimeField(null=True, blank=True)
    # Worded to match SentinelOne's own API vocabulary regardless of which
    # vendor actually reported the device (see AgentDevice's docstring in
    # agent_parity/models.py) — empty for missing_agent rows, same as vendor.
    platform = models.CharField(max_length=32, blank=True)
    machine_type = models.CharField(max_length=32, blank=True)
    # Unlike platform/machine_type, always one of the four defined choices —
    # every row gets a lifecycle classification, even "unknown", since
    # AD's own build number (see ADDevice's docstring) is captured for
    # every device, not just ones with a build-reporting agent.
    eol_status = models.CharField(
        max_length=16, choices=OSLifecycleStatus.choices, default=OSLifecycleStatus.UNKNOWN
    )
    # The Windows build number that determined eol_status, when one was
    # available (AD or SentinelOne) — null when neither side had one and
    # eol_status came from free-text matching instead.
    os_build = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["device__join_key", "vendor"]
        indexes = [models.Index(fields=["run", "status"])]

    def __str__(self):
        return f"{self.device.join_key} [{self.vendor or 'no vendor'}] {self.status}"
