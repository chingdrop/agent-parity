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
from dashboard.forms import (
    ClientForm,
    ConfigYAMLUploadForm,
    GlobalVendorAccountForm,
    VendorAccountNameForm,
    VendorCredentialForm,
)
from dashboard.models import Client, VendorCredential

GLOBAL_VENDORS = sorted(name for name, scope in VENDOR_SCOPE.items() if scope == "global")
PER_CLIENT_VENDORS = sorted(name for name, scope in VENDOR_SCOPE.items() if scope == "per_client")


def _upsert_client_vendor_row(client, vendor, existing_rows, credentials):
    """Update the one existing row for (client, vendor) client_form touches,
    or create it if this is the first time — shared by the per-client
    vendor (tenant credentials) and global vendor (account selection) save
    paths in ``client_form``, which never touch the same vendor's row."""
    row = existing_rows.get(vendor)
    if row:
        row.credentials = credentials
        row.save(update_fields=["credentials"])
    else:
        VendorCredential.objects.create(client=client, vendor=vendor, credentials=credentials)


@staff_member_required
def setup_overview(request):
    clients = Client.objects.all()
    # credentials is encrypted at rest (dashboard/fields.py) — there's no way
    # to check "has real values" at the DB level, so this decrypts each of
    # the (at most a handful of) global vendor rows in Python. A row can
    # exist with every value None (an imported config.yaml whose ${VAR}
    # refs were unset) — that's still "not configured," same as no row.
    global_rows = VendorCredential.objects.filter(client=None).order_by("vendor", "site_label")
    accounts_by_vendor: dict[str, list] = {vendor: [] for vendor in GLOBAL_VENDORS}
    for row in global_rows:
        accounts_by_vendor.setdefault(row.vendor, []).append(
            {"name": row.site_label, "configured": bool(any(row.credentials.values()))}
        )
    global_vendors = [
        {"name": vendor, "accounts": accounts_by_vendor[vendor]} for vendor in GLOBAL_VENDORS
    ]
    return render(
        request,
        "dashboard/setup/overview.html",
        {"clients": clients, "global_vendors": global_vendors},
    )


@staff_member_required
def client_form(request, slug: str | None = None):
    """Manages exactly one site/tenant row per vendor per client — a client
    with more than one (see agent_parity.config's multi-site/tenant support)
    can have several VendorCredential rows for the same (client, vendor)
    pair, distinguished by site_label. This form only ever touches the first
    one (by site_label/pk order); additional sites/tenants are added via
    config.yaml (re-)import or directly through admin. Not using
    update_or_create's (client, vendor) lookup here on purpose — with more
    than one matching row it would raise MultipleObjectsReturned.

    For global vendors (SentinelOne, BitDefender) that row doesn't hold
    credentials at all — those live on the vendor's own named-account rows
    (see ``vendor_credential_form``) — it holds only which account this
    client uses, via an "account" key merged in at ``AppConfig.sites_for()``
    time. Only matters once a vendor has more than one account.
    """
    client = get_object_or_404(Client, slug=slug) if slug else None
    existing_rows: dict[str, VendorCredential] = {}
    if client:
        for row in client.vendor_credentials.order_by("site_label", "pk"):
            existing_rows.setdefault(row.vendor, row)
    existing_creds = {vendor: row.credentials for vendor, row in existing_rows.items()}

    global_account_names = {
        vendor: list(
            VendorCredential.objects.filter(client=None, vendor=vendor)
            .order_by("site_label")
            .values_list("site_label", flat=True)
        )
        for vendor in GLOBAL_VENDORS
    }

    if request.method == "POST":
        form = ClientForm(request.POST, instance=client)
        vendor_forms = {
            vendor: VendorCredentialForm(vendor, request.POST, prefix=vendor)
            for vendor in PER_CLIENT_VENDORS
        }
        account_forms = {
            vendor: GlobalVendorAccountForm(
                vendor, global_account_names[vendor], request.POST, prefix=f"account-{vendor}"
            )
            for vendor in GLOBAL_VENDORS
        }
        if (
                form.is_valid()
                and all(f.is_valid() for f in vendor_forms.values())
                and all(f.is_valid() for f in account_forms.values())
        ):
            saved_client = form.save()
            for vendor, vendor_form in vendor_forms.items():
                if vendor not in saved_client.enabled_vendors:
                    continue
                merged = {**existing_creds.get(vendor, {}), **vendor_form.credentials()}
                _upsert_client_vendor_row(saved_client, vendor, existing_rows, merged)
            for vendor, account_form in account_forms.items():
                if vendor not in saved_client.enabled_vendors:
                    continue
                account = account_form.cleaned_data.get("account")
                existing = existing_creds.get(vendor, {})
                if account:
                    merged = {**existing, "account": account}
                elif "account" in existing:
                    merged = {k: v for k, v in existing.items() if k != "account"}
                else:
                    continue  # nothing selected, no existing row to touch either
                _upsert_client_vendor_row(saved_client, vendor, existing_rows, merged)
            return redirect("dashboard:setup_overview")
    else:
        form = ClientForm(instance=client)
        vendor_forms = {
            vendor: VendorCredentialForm(vendor, prefix=vendor) for vendor in PER_CLIENT_VENDORS
        }
        account_forms = {
            vendor: GlobalVendorAccountForm(
                vendor,
                global_account_names[vendor],
                prefix=f"account-{vendor}",
                initial={"account": existing_creds.get(vendor, {}).get("account", "")},
            )
            for vendor in GLOBAL_VENDORS
        }

    return render(
        request,
        "dashboard/setup/client_form.html",
        {
            "form": form,
            "vendor_forms": vendor_forms,
            "account_forms": account_forms,
            "client": client,
        },
    )


@staff_member_required
def vendor_credential_form(request, vendor: str, account: str):
    """Edits one named account's credentials (see
    ``agent_parity.config.VendorConfig.accounts``) — a global vendor can
    have more than one (real, historically separate SentinelOne consoles for
    MSSP vs. DFIR clients, for one). ``account`` doesn't have to already
    exist: saving upserts the row, so this is also how a brand-new account
    gets its first credentials (after ``vendor_account_create`` names it).
    """
    if vendor not in GLOBAL_VENDORS:
        raise Http404(f"{vendor!r} credentials are set per-client, not globally")

    existing = VendorCredential.objects.filter(client=None, vendor=vendor, site_label=account).first()
    existing_creds = existing.credentials if existing else {}

    if request.method == "POST":
        form = VendorCredentialForm(vendor, request.POST)
        if form.is_valid():
            merged = {**existing_creds, **form.credentials()}
            VendorCredential.objects.update_or_create(
                client=None, vendor=vendor, site_label=account, defaults={"credentials": merged}
            )
            return redirect("dashboard:setup_overview")
    else:
        form = VendorCredentialForm(vendor)

    return render(
        request,
        "dashboard/setup/vendor_credential_form.html",
        {"form": form, "vendor": vendor, "account": account},
    )


@staff_member_required
def vendor_account_create(request, vendor: str):
    """Names a brand-new account for a global vendor, then hands off to
    ``vendor_credential_form`` to actually set its credentials."""
    if vendor not in GLOBAL_VENDORS:
        raise Http404(f"{vendor!r} credentials are set per-client, not globally")

    if request.method == "POST":
        form = VendorAccountNameForm(request.POST)
        if form.is_valid():
            return redirect(
                "dashboard:vendor_credential_form", vendor=vendor, account=form.cleaned_data["account"]
            )
    else:
        form = VendorAccountNameForm()

    return render(
        request, "dashboard/setup/vendor_account_create.html", {"form": form, "vendor": vendor}
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
