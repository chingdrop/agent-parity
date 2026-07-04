"""Forms for the setup page — the only place vendor credentials are entered
by hand (everything else flows through ``manage.py import_config`` / this
page's own YAML upload, both of which call
``dashboard.config_db.import_app_config``).
"""

from __future__ import annotations

import re

from django import forms

from agent_parity.connectors import CONNECTOR_CLASSES
from dashboard.models import VENDOR_CHOICES, Client


class NewlineListField(forms.CharField):
    """A JSONField-backed list of strings, edited as one value per line.

    Used for ``Client.ad_target_devices`` — friendlier than the JSONField's
    default raw-JSON widget for something that's really just "one domain
    controller hostname per line, one per AD domain."
    """

    widget = forms.Textarea

    def prepare_value(self, value):
        # Called with the stored list on initial render, but with whatever
        # was last submitted (already a string) when redisplaying after a
        # validation error — handle both.
        if isinstance(value, list):
            return "\n".join(value)
        return value

    def to_python(self, value):
        if not value:
            return []
        return [line.strip() for line in value.splitlines() if line.strip()]


class ClientForm(forms.ModelForm):
    enabled_vendors = forms.MultipleChoiceField(
        choices=VENDOR_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Vendors this client uses. Global vendors' credentials are "
        "shared across every client (edited on the overview page); "
        "per-client vendors have a credentials section below.",
    )
    ad_target_devices = NewlineListField(
        required=True,
        help_text="One domain controller hostname per line — the export "
        "script runs on each and the results are concatenated into one "
        "master list. Most clients have just one.",
    )

    class Meta:
        model = Client
        fields = [
            "name",
            "slug",
            "is_active",
            "ad_target_devices",
            "sync_interval_hours",
            "enabled_vendors",
        ]


class VendorCredentialForm(forms.Form):
    """One CharField per ``CONNECTOR_CLASSES[vendor].required_credentials``
    (e.g. api_url/api_token for SentinelOne) — built dynamically so the
    field set can never drift from what the connector actually requires.

    Fields are always rendered blank, even when editing an existing
    credential: leaving a field blank on submit means "keep the current
    value," so a stored secret is never echoed back into the page.
    """

    def __init__(self, vendor: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vendor = vendor
        for field_name in CONNECTOR_CLASSES[vendor].required_credentials:
            self.fields[field_name] = forms.CharField(
                required=False, widget=forms.PasswordInput(render_value=False)
            )

    def credentials(self) -> dict:
        """Only the fields the user actually typed something into — the
        view merges this over the existing stored credentials."""
        return {name: value for name, value in self.cleaned_data.items() if value}


class GlobalVendorAccountForm(forms.Form):
    """Which of a global vendor's named accounts a client uses — e.g.
    SentinelOne's separate "mssp"/"dfir" consoles (see
    ``agent_parity.config.VendorConfig.accounts``). Only meaningful once a
    vendor has more than one account; left blank, a vendor with exactly one
    account resolves to it implicitly (``AppConfig._resolve_account``).
    """

    def __init__(self, vendor: str, account_names: list[str], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vendor = vendor
        self.fields["account"] = forms.ChoiceField(
            choices=[("", "(default — only matters with more than one account)")]
            + [(name, name) for name in account_names],
            required=False,
            label=f"{vendor} account",
        )


class VendorAccountNameForm(forms.Form):
    """Names a brand-new account for a global vendor (SentinelOne, BitDefender)
    before handing off to the per-account credential form."""

    account = forms.CharField(
        max_length=64,
        help_text='A short name for this account, e.g. "mssp" or "dfir" — '
        "used in the URL, not shown anywhere else.",
    )

    def clean_account(self) -> str:
        value = self.cleaned_data["account"].strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise forms.ValidationError(
                "Use only letters, numbers, hyphens, and underscores."
            )
        return value


class ConfigYAMLUploadForm(forms.Form):
    config_file = forms.FileField(label="config.yaml")
