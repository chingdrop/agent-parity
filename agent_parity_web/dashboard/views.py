"""Dashboard views.

Aggregation happens in the ORM (``Count`` + ``filter=Q(...)`` conditional
aggregates) — the views never re-derive pandas classification logic; they
only present what the pipeline already persisted.
"""

from dashboard.models import Client, CorrelationRun, CoverageSnapshot, CoverageStatus, Device
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render

#: Statuses that describe an AD-known device (the coverage denominator);
#: orphaned agents are a finding, not a coverage gap.
AD_STATUSES = (
    CoverageStatus.COVERED,
    CoverageStatus.STALE_COVERAGE,
    CoverageStatus.MISSING_AGENT,
)


def _latest_run(client: Client) -> CorrelationRun | None:
    return (
        client.runs.exclude(status=CorrelationRun.RunStatus.PENDING)
        .order_by("-started_at")
        .first()
    )


def _coverage_pct(counts: dict) -> float | None:
    denominator = sum(counts.get(status, 0) for status in AD_STATUSES)
    if not denominator:
        return None
    return round(100.0 * counts.get(CoverageStatus.COVERED, 0) / denominator, 1)


def overview(request):
    cards = []
    for client in Client.objects.filter(is_active=True):
        run = _latest_run(client)
        if run is None:
            cards.append({"client": client, "run": None})
            continue

        counts = {
            row["status"]: row["n"]
            for row in run.snapshots.values("status").annotate(n=Count("id"))
        }
        # Servers stand in for "high-value assets" (Domain Controllers,
        # file/storage servers, ...) — a Windows Server SKU is a reliable
        # signal on its own; hostname naming conventions aren't, so
        # machine_type (not a name pattern) is what this filters on.
        server_counts = {
            row["status"]: row["n"]
            for row in run.snapshots.filter(machine_type="server")
            .values("status")
            .annotate(n=Count("id"))
        }
        vendors = []
        vendor_rows = (
            run.snapshots.exclude(vendor="")
            .values("vendor", "status")
            .annotate(n=Count("id"))
        )
        by_vendor: dict[str, dict] = {}
        for row in vendor_rows:
            by_vendor.setdefault(row["vendor"], {})[row["status"]] = row["n"]
        for vendor, vcounts in sorted(by_vendor.items()):
            covered = vcounts.get(CoverageStatus.COVERED, 0)
            stale = vcounts.get(CoverageStatus.STALE_COVERAGE, 0)
            vendors.append(
                {
                    "name": vendor,
                    "counts": vcounts,
                    # Of this vendor's matched agents, how many check in healthily.
                    "healthy_pct": round(100.0 * covered / (covered + stale), 1)
                    if (covered + stale)
                    else None,
                }
            )

        cards.append(
            {
                "client": client,
                "run": run,
                "counts": counts,
                "coverage_pct": _coverage_pct(counts),
                "server_counts": server_counts,
                "server_coverage_pct": _coverage_pct(server_counts),
                "vendors": vendors,
            }
        )
    return render(
        request,
        "dashboard/overview.html",
        {"cards": cards, "statuses": CoverageStatus},
    )


def device_list(request):
    clients = Client.objects.filter(is_active=True)
    latest_run_ids = [run.pk for c in clients if (run := _latest_run(c))]

    snapshots = (
        CoverageSnapshot.objects.filter(run_id__in=latest_run_ids)
        .select_related("device", "device__client", "run")
        .order_by("device__join_key", "vendor")
    )

    selected = {
        "client": request.GET.get("client", ""),
        "status": request.GET.get("status", ""),
        "vendor": request.GET.get("vendor", ""),
        "machine_type": request.GET.get("machine_type", ""),
    }
    if selected["client"]:
        snapshots = snapshots.filter(device__client__slug=selected["client"])
    if selected["status"]:
        snapshots = snapshots.filter(status=selected["status"])
    if selected["vendor"]:
        snapshots = snapshots.filter(vendor=selected["vendor"])
    if selected["machine_type"]:
        snapshots = snapshots.filter(machine_type=selected["machine_type"])

    page = Paginator(snapshots, 50).get_page(request.GET.get("page"))
    vendor_names = (
        CoverageSnapshot.objects.exclude(vendor="")
        .values_list("vendor", flat=True)
        .distinct()
        .order_by("vendor")
    )
    machine_types = (
        CoverageSnapshot.objects.exclude(machine_type="")
        .values_list("machine_type", flat=True)
        .distinct()
        .order_by("machine_type")
    )
    return render(
        request,
        "dashboard/device_list.html",
        {
            "page": page,
            "clients": clients,
            "statuses": CoverageStatus.choices,
            "vendors": vendor_names,
            "machine_types": machine_types,
            "selected": selected,
        },
    )


def device_detail(request, pk: int):
    device = get_object_or_404(Device.objects.select_related("client"), pk=pk)
    history = device.snapshots.select_related("run").order_by("-run__started_at", "vendor")
    return render(
        request,
        "dashboard/device_detail.html",
        {"device": device, "history": history},
    )


def trend_data(request, slug: str):
    """Coverage % per CorrelationRun, consumed by the overview Chart.js chart.

    Reports overall coverage and server-only coverage (the high-value-asset
    stand-in) as two parallel series — a quarterly report needs to show both
    "coverage is improving overall" and "the assets that matter most are
    covered" as trends, not just a single point-in-time snapshot.
    """
    client = get_object_or_404(Client, slug=slug)
    runs = (
        client.runs.exclude(status=CorrelationRun.RunStatus.PENDING)
        .order_by("started_at")
        .annotate(
            covered=Count("snapshots", filter=Q(snapshots__status=CoverageStatus.COVERED)),
            stale=Count("snapshots", filter=Q(snapshots__status=CoverageStatus.STALE_COVERAGE)),
            missing=Count("snapshots", filter=Q(snapshots__status=CoverageStatus.MISSING_AGENT)),
            server_covered=Count(
                "snapshots",
                filter=Q(snapshots__status=CoverageStatus.COVERED, snapshots__machine_type="server"),
            ),
            server_stale=Count(
                "snapshots",
                filter=Q(
                    snapshots__status=CoverageStatus.STALE_COVERAGE, snapshots__machine_type="server"
                ),
            ),
            server_missing=Count(
                "snapshots",
                filter=Q(
                    snapshots__status=CoverageStatus.MISSING_AGENT, snapshots__machine_type="server"
                ),
            ),
        )
    )
    labels, values, server_values = [], [], []
    for run in runs:
        denominator = run.covered + run.stale + run.missing
        if not denominator:
            continue
        labels.append(run.started_at.strftime("%Y-%m-%d %H:%M"))
        values.append(round(100.0 * run.covered / denominator, 1))
        server_denominator = run.server_covered + run.server_stale + run.server_missing
        server_values.append(
            round(100.0 * run.server_covered / server_denominator, 1) if server_denominator else None
        )
    return JsonResponse(
        {
            "client": client.slug,
            "labels": labels,
            "coverage_pct": values,
            "server_coverage_pct": server_values,
        }
    )
