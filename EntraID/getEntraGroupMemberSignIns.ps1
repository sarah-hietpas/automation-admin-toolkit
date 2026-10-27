<#
.SYNOPSIS
    Reports account status and last sign-in dates for every user in an Entra ID group.

.DESCRIPTION
    Uses the Microsoft Graph PowerShell SDK to enumerate a group's members (direct or
    transitive/nested), then pulls AccountEnabled and signInActivity for each user.
    Results are shown in the console and exported to CSV.

.REQUIREMENTS
    - Module: Microsoft.Graph.Users, Microsoft.Graph.Groups
        Install-Module Microsoft.Graph -Scope CurrentUser
    - Delegated scopes: User.Read.All, GroupMember.Read.All, AuditLog.Read.All, Organization.Read.All
    - signInActivity requires Entra ID P1/P2 licensing in the tenant, and the signed-in
      admin needs a role that can read sign-in data (e.g. Reports Reader, Global Reader,
      Security Reader).

.EXAMPLE
    .\Get-EntraGroupMemberSignIns.ps1 -GroupName "Sales Team"

.EXAMPLE
    .\Get-EntraGroupMemberSignIns.ps1 -GroupId "0a1b2c3d-..." -Transitive -ExportPath "C:\Reports\sales.csv"
#>

[CmdletBinding(DefaultParameterSetName = 'ByName')]
param(
    [Parameter(Mandatory, ParameterSetName = 'ByName')]
    [string]$GroupName,

    [Parameter(Mandatory, ParameterSetName = 'ById')]
    [string]$GroupId,

    # Include members of nested groups
    [switch]$Transitive,

    [string]$ExportPath = ".\GroupMemberSignIns_$(Get-Date -Format 'yyyyMMdd_HHmm').csv",

    # Flag users with no successful sign-in in this many days
    [int]$StaleDays = 90,

    # Skip downloading Microsoft's friendly license name list (show SKU part numbers only)
    [switch]$NoFriendlyLicenseNames
)

# --- Connect ---------------------------------------------------------------
$scopes = 'User.Read.All', 'GroupMember.Read.All', 'AuditLog.Read.All', 'Organization.Read.All'
$ctx = Get-MgContext
if (-not $ctx -or ($scopes | Where-Object { $_ -notin $ctx.Scopes })) {
    Connect-MgGraph -Scopes $scopes -NoWelcome
}

# --- Build license lookup (SkuId -> name) ---------------------------------
# Tenant SKUs give the SkuPartNumber (e.g. SPE_E3). Microsoft's published CSV
# adds friendly names (e.g. "Microsoft 365 E3"); falls back if unavailable.
$skuMap = @{}
Get-MgSubscribedSku -All | ForEach-Object { $skuMap[$_.SkuId.ToString()] = $_.SkuPartNumber }

$friendlyMap = @{}
if (-not $NoFriendlyLicenseNames) {
    try {
        $csvUrl = 'https://download.microsoft.com/download/e/3/e/e3e9faf2-f28b-490a-9ada-c6089a1fc5b0/Product%20names%20and%20service%20plan%20identifiers%20for%20licensing.csv'
        $tmp = Join-Path $env:TEMP 'ms_license_names.csv'
        Invoke-WebRequest -Uri $csvUrl -OutFile $tmp -UseBasicParsing -ErrorAction Stop
        Import-Csv $tmp | ForEach-Object {
            if (-not $friendlyMap.ContainsKey($_.GUID)) { $friendlyMap[$_.GUID] = $_.Product_Display_Name }
        }
    }
    catch {
        Write-Warning "Couldn't download friendly license names; using SKU part numbers instead."
    }
}

function Resolve-LicenseName([string]$SkuId) {
    if ($friendlyMap.ContainsKey($SkuId)) { return $friendlyMap[$SkuId] }
    if ($skuMap.ContainsKey($SkuId))      { return $skuMap[$SkuId] }
    return $SkuId
}

# --- Resolve group ---------------------------------------------------------
if ($PSCmdlet.ParameterSetName -eq 'ByName') {
    $safeName = $GroupName.Replace("'", "''")
    $group = Get-MgGroup -Filter "displayName eq '$safeName'" -All
    if (-not $group)          { throw "No group found named '$GroupName'." }
    if ($group.Count -gt 1)   { throw "Multiple groups named '$GroupName'. Use -GroupId instead." }
} else {
    $group = Get-MgGroup -GroupId $GroupId -ErrorAction Stop
}
Write-Host "Group: $($group.DisplayName) ($($group.Id))" -ForegroundColor Cyan

# --- Get user members ------------------------------------------------------
$members = if ($Transitive) {
    Get-MgGroupTransitiveMember -GroupId $group.Id -All
} else {
    Get-MgGroupMember -GroupId $group.Id -All
}

# Keep only user objects (skip devices, service principals, nested groups)
$userIds = $members |
    Where-Object { $_.AdditionalProperties.'@odata.type' -eq '#microsoft.graph.user' } |
    Select-Object -ExpandProperty Id -Unique

Write-Host "Found $($userIds.Count) user(s). Pulling sign-in data..." -ForegroundColor Cyan

# --- Pull status + sign-in activity ----------------------------------------
$props = 'Id','DisplayName','UserPrincipalName','Mail','AccountEnabled','UserType',
         'CreatedDateTime','SignInActivity','AssignedLicenses'
$cutoff = (Get-Date).AddDays(-$StaleDays)
$i = 0

$report = foreach ($id in $userIds) {
    $i++
    Write-Progress -Activity "Querying users" -Status "$i of $($userIds.Count)" `
        -PercentComplete (($i / $userIds.Count) * 100)

    try {
        $u = Get-MgUser -UserId $id -Property $props -ErrorAction Stop
        $sia = $u.SignInActivity

        # Most recent of interactive / non-interactive
        $lastAny = @($sia.LastSignInDateTime, $sia.LastNonInteractiveSignInDateTime) |
            Where-Object { $_ } | Sort-Object -Descending | Select-Object -First 1

        $lastSuccess = $sia.LastSuccessfulSignInDateTime

        $licenses = @($u.AssignedLicenses | ForEach-Object { Resolve-LicenseName $_.SkuId.ToString() }) |
            Sort-Object

        [PSCustomObject]@{
            DisplayName              = $u.DisplayName
            UserPrincipalName        = $u.UserPrincipalName
            Mail                     = $u.Mail
            UserType                 = $u.UserType
            Status                   = if ($u.AccountEnabled) { 'Enabled' } else { 'Disabled' }
            Licensed                 = $licenses.Count -gt 0
            Licenses                 = $licenses -join '; '
            LastInteractiveSignIn    = $sia.LastSignInDateTime
            LastNonInteractiveSignIn = $sia.LastNonInteractiveSignInDateTime
            LastSuccessfulSignIn     = $lastSuccess
            LastSignInAny            = $lastAny
            DaysSinceLastSuccess     = if ($lastSuccess) { [int]((Get-Date) - $lastSuccess).TotalDays } else { $null }
            Stale                    = (-not $lastSuccess) -or ($lastSuccess -lt $cutoff)
            CreatedDateTime          = $u.CreatedDateTime
        }
    }
    catch {
        Write-Warning "Failed to read user $id : $($_.Exception.Message)"
    }
}
Write-Progress -Activity "Querying users" -Completed

# --- Output ----------------------------------------------------------------
$report = $report | Sort-Object Status, LastSuccessfulSignIn
$report | Format-Table DisplayName, UserPrincipalName, Status, Licenses, LastSuccessfulSignIn, DaysSinceLastSuccess, Stale -AutoSize
$report | Export-Csv -Path $ExportPath -NoTypeInformation -Encoding UTF8

$enabled  = ($report | Where-Object Status -eq 'Enabled').Count
$disabled = ($report | Where-Object Status -eq 'Disabled').Count
$stale    = ($report | Where-Object Stale).Count
$unlic    = ($report | Where-Object { -not $_.Licensed }).Count
$staleLic = ($report | Where-Object { $_.Stale -and $_.Licensed }).Count
Write-Host "`nEnabled: $enabled | Disabled: $disabled | Unlicensed: $unlic | No successful sign-in in $StaleDays+ days: $stale" -ForegroundColor Yellow
Write-Host "Stale users still holding a license (reclaim candidates): $staleLic" -ForegroundColor Yellow
Write-Host "Exported to $ExportPath" -ForegroundColor Green
