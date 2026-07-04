<#
.SYNOPSIS
    Export Active Directory computer objects as CSV, either to stdout or
    directly to object storage.

.DESCRIPTION
    Deployed to a domain-joined endpoint via a security vendor's remote
    script execution (SentinelOne RSO or Carbon Black Live Response) and
    executed there — it never runs on the agent-parity host itself.

    When object storage is configured for the client (see
    agent_parity.storage / deployment.script_runner), the caller passes a
    short-lived, single-object presigned PUT URL as -UploadUrl: this script
    uploads its CSV directly there instead of returning it through the
    vendor's remote-execution channel. That channel has real output-size
    limits a full AD export can exceed, so a large environment's export
    never needs to fit through it at all. Without -UploadUrl, output goes to
    stdout exactly as before object storage existed.

    LastLogonTimestamp is converted from Windows FILETIME to an ISO-8601
    UTC string so the collector side never has to deal with FILETIME.
#>

param(
    [string]$UploadUrl
)

Import-Module ActiveDirectory -ErrorAction Stop

$rows = Get-ADComputer -Filter * -Properties DNSHostName, OperatingSystem, LastLogonTimestamp, Enabled |
    Select-Object Name,
        DNSHostName,
        @{Name = 'OperatingSystem'; Expression = { $_.OperatingSystem }},
        @{Name = 'LastLogonTimestamp'; Expression = {
            if ($_.LastLogonTimestamp) {
                [DateTime]::FromFileTimeUtc($_.LastLogonTimestamp).ToString('o')
            }
        }},
        Enabled,
        DistinguishedName

$csv = $rows | ConvertTo-Csv -NoTypeInformation

if ($UploadUrl) {
    # Presigned S3/MinIO PUT URLs accept a plain PUT of the object bytes; no
    # storage credentials ever touch this endpoint, and the URL expires in
    # minutes and can write exactly this one object.
    Invoke-RestMethod -Uri $UploadUrl -Method Put -Body ($csv -join "`n") -ContentType 'text/csv'
    Write-Output "Uploaded $($rows.Count) AD computer object(s) to object storage."
} else {
    $csv
}
