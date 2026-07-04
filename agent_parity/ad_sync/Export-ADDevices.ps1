<#
.SYNOPSIS
    Export Active Directory computer objects as CSV on stdout.

.DESCRIPTION
    Deployed to a domain-joined endpoint via a security vendor's remote
    script execution (SentinelOne RSO, Carbon Black Live Response, or
    BitDefender GravityZone task) and executed there — it never runs on the
    agent-parity host itself, which is why it needs no parameters and writes
    only to stdout: the vendor's remote-execution channel carries the output
    back.

    LastLogonTimestamp is converted from Windows FILETIME to an ISO-8601
    UTC string so the collector side never has to deal with FILETIME.
#>

Import-Module ActiveDirectory -ErrorAction Stop

Get-ADComputer -Filter * -Properties DNSHostName, OperatingSystem, LastLogonTimestamp, Enabled |
    Select-Object Name,
        DNSHostName,
        @{Name = 'OperatingSystem'; Expression = { $_.OperatingSystem }},
        @{Name = 'LastLogonTimestamp'; Expression = {
            if ($_.LastLogonTimestamp) {
                [DateTime]::FromFileTimeUtc($_.LastLogonTimestamp).ToString('o')
            }
        }},
        Enabled,
        DistinguishedName |
    ConvertTo-Csv -NoTypeInformation
