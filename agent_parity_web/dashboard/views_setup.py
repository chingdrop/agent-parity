"""The setup page: manage client topology and vendor credentials without
hand-editing config.yaml. Two ways in: a manual add/edit form, or a one-time
config.yaml upload (both end up calling the same DB rows config_db.py's
``import_app_config`` writes for the CLI path).

Gated behind ``staff_member_required`` — unlike the read-only dashboard
views, these edit and persist vendor credentials, so they get the same bar
as ``/admin/``, the only other privileged surface in this app.
"""

from __future__ import annotations

import tempfile

import yaml
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from agent_parity.config import VENDOR_SCOPE, ConfigError, load_config
from dashboard.config_db import import_app_config
from dashboard.forms import ClientForm, ConfigYAMLUploadForm, VendorCredentialForm
from dashboard.models import Client, VendorCredential

GLOBAL_VENDORS = sorted(name for name, scope in VENDOR_SCOPE.items() if scope == "global")
PER_CLIENT_VENDORS = sorted(name for name, scope in VENDOR_SCOPE.items() if scope == "per_client")


@staff_member_required
def setup_overview(request):
    clients = Client.objects.all()
    # credentials is encrypted at rest (dashboard/fields.py) — there's no way
    # to check "has real values" at the DB level, so this decrypts each of
    # the (at most a handful of) global vendor rows in Python. A row can
    # exist with every value None (an imported config.yaml whose ${VAR}
    # refs were unset) — that's still "not configured," same as no row.
    global_credential_rows = {row.vendor: row for row in VendorCredential.objects.filter(client=None)}
    global_vendors = []
    for vendor in GLOBAL_VENDORS:
        row = global_credential_rows.get(vendor)
        configured = bool(row and any(row.credentials.values()))
        global_vendors.append({"name": vendor, "configured": configured})
    return render(
        request,
        "dashboard/setup/overview.html",
        {"clients": clients, "global_vendors": global_vendors},
    )


@staff_member_required
def client_form(request, slug: str | None = None):
    """Manages exactly one site/tenant per vendor per client — a client with
    more than one (see agent_parity.config's multi-site/tenant support) can
    have several VendorCredential rows for the same (client, vendor) pair,
    distinguished by site_label. This form only ever touches the first one
    (by site_label/pk order); additional sites/tenants are added via
    config.yaml (re-)import or directly through admin. Not using
    update_or_create's (client, vendor) lookup here on purpose — with more
    than one matching row it would raise MultipleObjectsReturned.
    """
    client = get_object_or_404(Client, slug=slug) if slug else None
    existing_rows: dict[str, VendorCredential] = {}
    if client:
        for row in client.vendor_credentials.order_by("site_label", "pk"):
            existing_rows.setdefault(row.vendor, row)
    existing_creds = {vendor: row.credentials for vendor, row in existing_rows.items()}

    if request.method == "POST":
        form = ClientForm(request.POST, instance=client)
        vendor_forms = {
            vendor: VendorCredentialForm(vendor, request.POST, prefix=vendor)
            for vendor in PER_CLIENT_VENDORS
        }
        if form.is_valid() and all(f.is_valid() for f in vendor_forms.values()):
            saved_client = form.save()
            for vendor, vendor_form in vendor_forms.items():
                if vendor not in saved_client.enabled_vendors:
                    continue
                merged = {**existing_creds.get(vendor, {}), **vendor_form.credentials()}
                row = existing_rows.get(vendor)
                if row:
                    row.credentials = merged
                    row.save(update_fields=["credentials"])
                else:
                    VendorCredential.objects.create(
                        client=saved_client, vendor=vendor, credentials=merged
                    )
            return redirect("dashboard:setup_overview")
    else:
        form = ClientForm(instance=client)
        vendor_forms = {
            vendor: VendorCredentialForm(vendor, prefix=vendor) for vendor in PER_CLIENT_VENDORS
        }

    return render(
        request,
        "dashboard/setup/client_form.html",
        {"form": form, "vendor_forms": vendor_forms, "client": client},
    )


@staff_member_required
def vendor_credential_form(request, vendor: str):
    if vendor not in GLOBAL_VENDORS:
        raise Http404(f"{vendor!r} credentials are set per-client, not globally")

    existing = VendorCredential.objects.filter(client=None, vendor=vendor).first()
    existing_creds = existing.credentials if existing else {}

    if request.method == "POST":
        form = VendorCredentialForm(vendor, request.POST)
        if form.is_valid():
            merged = {**existing_creds, **form.credentials()}
            VendorCredential.objects.update_or_create(
                client=None, vendor=vendor, defaults={"credentials": merged}
            )
            return redirect("dashboard:setup_overview")
    else:
        form = VendorCredentialForm(vendor)

    return render(
        request, "dashboard/setup/vendor_credential_form.html", {"form": form, "vendor": vendor}
    )


@staff_member_required
def import_config_yaml(request):
    if request.method == "POST":
        form = ConfigYAMLUploadForm(request.POST, request.FILES)
        if form.is_valid():
            with tempfile.NamedTemporaryFile(suffix=".yaml") as tmp:
                for chunk in request.FILES["config_file"].chunks():
                    tmp.write(chunk)
                tmp.flush()
                try:
                    config = load_config(path=tmp.name)
                except (ConfigError, yaml.YAMLError, AttributeError, TypeError) as exc:
                    # The uploaded file's content is arbitrary — any parse
                    # failure becomes a form error, not a 500.
                    form.add_error("config_file", f"Could not parse this file: {exc}")
                else:
                    import_app_config(config)
                    return redirect("dashboard:setup_overview")
    else:
        form = ConfigYAMLUploadForm()

    return render(request, "dashboard/setup/import.html", {"form": form})
