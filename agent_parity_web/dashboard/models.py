"""Relational schema for correlation results.

A Client owns Devices; every pipeline execution is a CorrelationRun; each run
produces one CoverageSnapshot per device/vendor observation, FK'd to both —
so per-device history and per-run aggregates are both plain queries.
"""

from django.db import models
from django.utils import timezone

from agent_parity.models import CoverageStatus as PipelineStatus


class CoverageStatus(models.TextChoices):
    """ORM mirror of the pipeline's CoverageStatus enum (same values)."""

    COVERED = PipelineStatus.COVERED.value, "Covered"
    MISSING_AGENT = PipelineStatus.MISSING_AGENT.value, "Missing agent"
    ORPHANED_AGENT = PipelineStatus.ORPHANED_AGENT.value, "Orphaned agent"
    STALE_COVERAGE = PipelineStatus.STALE_COVERAGE.value, "Stale coverage"


class Client(models.Model):
    """One managed organization/environment."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(default=True)
    # Denormalized from config.yaml on each sync, for display/filtering.
    enabled_vendors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


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

    class Meta:
        ordering = ["device__join_key", "vendor"]
        indexes = [models.Index(fields=["run", "status"])]

    def __str__(self):
        return f"{self.device.join_key} [{self.vendor or 'no vendor'}] {self.status}"
