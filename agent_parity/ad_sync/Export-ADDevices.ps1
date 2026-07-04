<#
.SYNOPSIS
    Export Active Directory computer objects as CSV directly to object storage.

.DESCRIPTION
    Deployed to a domain-joined endpoint via a security vendor's remote
    script execution (SentinelOne RSO or Carbon Black Live Response) and
    executed there — it never runs on the agent-parity host itself.

    -UploadUrl is a short-lived, single-object presigned PUT URL (see
    agent_parity.storage / deployment.script_runner) that this script uploads
    its CSV to directly, rather than returning it through the vendor's own
    remote-execution output channel. That channel is not a reliable way to
    get a full AD export back: RSO/Live Response output handling doesn't
    consistently preserve exact formatting (encoding, line endings) and has
    real size limits a large environment's export can exceed. The uploaded
    bytes are exactly what this script wrote — nothing downstream re-encodes
    or truncates them.

    LastLogonTimestamp is converted from Windows FILETIME to an ISO-8601
    UTC string so the collector side never has to deal with FILETIME.
#>

param(
    [Parameter(Mandatory = $true)]
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

# Presigned S3/MinIO PUT URLs accept a plain PUT of the object bytes; no
# storage credentials ever touch this endpoint, and the URL expires in
# minutes and can write exactly this one object.
Invoke-RestMethod -Uri $UploadUrl -Method Put -Body ($csv -join "`n") -ContentType 'text/csv'
Write-Output "Uploaded $($rows.Count) AD computer object(s) to object storage."
