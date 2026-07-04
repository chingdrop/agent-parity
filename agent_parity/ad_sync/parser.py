"""Parse raw Export-ADDevices.ps1 output into a normalized DataFrame.

The script emits CSV on stdout with columns:
Name, DNSHostName, OperatingSystem, OperatingSystemVersion,
LastLogonTimestamp, Enabled, DistinguishedName. This parser is the only
place that knows those column names — everything downstream works with the
normalized frame it returns.
"""

from __future__ import annotations

import io

import pandas as pd

from agent_parity.models import normalize_hostname
from agent_parity.os_eol import extract_build_number

#: Normalized output columns, in order.
AD_COLUMNS = [
    "join_key",
    "hostname",
    "dns_hostname",
    "os",
    "os_build",
    "last_logon",
    "enabled",
    "distinguished_name",
]


class ADParseError(Exception):
    pass


def parse_ad_export(raw_csv: str) -> pd.DataFrame:
    """Raw script stdout -> DataFrame with one row per AD computer object."""
    try:
        frame = pd.read_csv(io.StringIO(raw_csv), dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ADParseError(f"AD export is not parseable CSV: {exc}") from exc

    if "Name" not in frame.columns:
        raise ADParseError(
            f"AD export is missing the 'Name' column (got: {list(frame.columns)})"
        )

    parsed = pd.DataFrame(
        {
            "hostname": frame["Name"].str.strip(),
            "dns_hostname": frame["DNSHostName"].astype(str) if "DNSHostName" in frame else "",
            "os": frame["OperatingSystem"] if "OperatingSystem" in frame else "",
            "os_build": (
                frame["OperatingSystemVersion"].map(extract_build_number)
                if "OperatingSystemVersion" in frame
                else None
            ),
            "last_logon": pd.to_datetime(
                frame["LastLogonTimestamp"], errors="coerce", utc=True, format="ISO8601"
            )
            if "LastLogonTimestamp" in frame
            else pd.NaT,
            "enabled": frame["Enabled"].str.lower().eq("true") if "Enabled" in frame else True,
            "distinguished_name": frame["DistinguishedName"] if "DistinguishedName" in frame else "",
        }
    )
    parsed["join_key"] = parsed["hostname"].map(normalize_hostname)

    # Drop rows with no usable hostname rather than let them create a bogus
    # empty-string join group.
    parsed = parsed[parsed["join_key"] != ""].reset_index(drop=True)
    return parsed[AD_COLUMNS]
