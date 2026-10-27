<#
.SYNOPSIS
    Walks through creating a new user in Microsoft 365.

.DESCRIPTION
    Prompts for the new hire's details, shows a summary to confirm, then:
      1. Creates the account
           Display name : First Last
           Username     : first.last@udomain.com
           Country      : United States (usage location US)
      2. Assigns the Microsoft 365 E5 license
      3. Sets the manager
      4. Adds the user to the standard onboarding groups

    Uses the Microsoft Graph PowerShell SDK. Mail-enabled groups (distribution lists and
    mail-enabled security groups) can't be changed through Graph, so for those the script
    uses Exchange Online if the ExchangeOnlineManagement module is installed, or prints
    the command to run later.

    Run as an account with User Administrator (or higher) plus rights to manage the
    groups listed in Settings.

.NOTES
    One-time setup (if not already installed):
        Install-Module Microsoft.Graph -Scope CurrentUser
        Install-Module ExchangeOnlineManagement -Scope CurrentUser   # optional, for mail-enabled groups

.EXAMPLE
    .\newUserCreation.ps1
#>

#Requires -Version 5.1

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive script: colored console output is intended.')]
param()

# ================================ Settings ================================

$Domain        = 'domain.com'
$Country       = 'United States'
$UsageLocation = 'US'      # Microsoft requires a usage location before a license can be assigned
$ForcePasswordChangeAtFirstSignIn = $true

# E5 license SKUs to look for. If exactly one is in the tenant it's used automatically;
# otherwise you'll get a list to pick from.
$E5SkuPartNumbers = @(
    'SPE_E5'                          # Microsoft 365 E5
    'SPE_E5_NOPSTNCONF'               # Microsoft 365 E5 without Audio Conferencing
    'Microsoft_365_E5_(no_Teams)'     # Microsoft 365 E5 (no Teams)
    'ENTERPRISEPREMIUM'               # Office 365 E5
    'ENTERPRISEPREMIUM_NOPSTNCONF'    # Office 365 E5 without Audio Conferencing
    'Office_365_E5_(no_Teams)'        # Office 365 E5 (no Teams)
)

$OnboardingGroups = @(
    'Group1'
    'Group2'
    'Group3'
    'EmailDistributionList1'
    'EmailDistributionList2'
)

# How long to wait for Exchange Online to see the new mailbox before adding it to
# mail-enabled groups.
$ExchangeWaitMinutes = 10

$GraphScopes = @(
    'User.ReadWrite.All'          # create user, set manager, assign license
    'Group.Read.All'              # look up groups
    'GroupMember.ReadWrite.All'   # add group members
    'Organization.Read.All'       # read license SKUs
)

$GraphModules = @(
    'Microsoft.Graph.Authentication'
    'Microsoft.Graph.Users'
    'Microsoft.Graph.Users.Actions'
    'Microsoft.Graph.Groups'
    'Microsoft.Graph.Identity.DirectoryManagement'
)

# ================================ Helpers =================================

function Write-Section {
    param([string]$Text)
    Write-Host ''
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Read-Required {
    param([string]$Prompt)
    do {
        $value = ([string](Read-Host $Prompt)).Trim()
        if (-not $value) { Write-Host '  This field is required.' -ForegroundColor Yellow }
    } while (-not $value)
    return $value
}

function Read-YesNo {
    param([string]$Prompt, [bool]$Default = $true)
    $suffix = '[y/N]'
    if ($Default) { $suffix = '[Y/n]' }
    while ($true) {
        $answer = ([string](Read-Host "$Prompt $suffix")).Trim().ToLower()
        if (-not $answer)               { return $Default }
        if ($answer -in @('y', 'yes'))  { return $true }
        if ($answer -in @('n', 'no'))   { return $false }
    }
}

function ConvertTo-UsernamePart {
    # Lowercases, strips accents, and removes spaces/apostrophes/other symbols (keeps hyphens).
    # e.g. "Mary Ann" -> "maryann", "O'Brien" -> "obrien"
    param([string]$Text)
    $decomposed = $Text.Normalize([Text.NormalizationForm]::FormD)
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $decomposed.ToCharArray()) {
        if ([Globalization.CharUnicodeInfo]::GetUnicodeCategory($ch) -ne [Globalization.UnicodeCategory]::NonSpacingMark) {
            [void]$sb.Append($ch)
        }
    }
    return ($sb.ToString().ToLower() -replace '[^a-z0-9-]', '')
}

function Test-AddressInUse {
    param([string]$Address)
    $safe = $Address.Replace("'", "''")
    $hit = Get-MgUser -Filter "userPrincipalName eq '$safe' or mail eq '$safe'" -Property Id -Top 1 -ErrorAction SilentlyContinue
    return [bool]$hit
}

function ConvertFrom-SecurePassword {
    # Microsoft Graph only accepts the new user's password as plain text, so this is
    # called once, right as the request is sent. Everywhere else it stays a SecureString.
    param([SecureString]$SecurePassword)
    return [System.Net.NetworkCredential]::new('', $SecurePassword).Password
}

function Test-PasswordComplexity {
    # Matches the Entra ID default: 8-256 characters, 3 of the 4 character types.
    param([SecureString]$Password)
    $length = $Password.Length
    if ($length -lt 8 -or $length -gt 256) { return $false }
    $plain = ConvertFrom-SecurePassword $Password
    $types = 0
    if ($plain -cmatch '[a-z]')        { $types++ }
    if ($plain -cmatch '[A-Z]')        { $types++ }
    if ($plain -match  '[0-9]')        { $types++ }
    if ($plain -match  '[^a-zA-Z0-9]') { $types++ }
    $plain = $null
    return ($types -ge 3)
}

function Test-SecurePasswordMatch {
    param([SecureString]$First, [SecureString]$Second)
    return ((ConvertFrom-SecurePassword $First) -ceq (ConvertFrom-SecurePassword $Second))
}

function Read-NewPassword {
    # Returns a SecureString.
    while ($true) {
        $first  = Read-Host 'Password' -AsSecureString
        $second = Read-Host 'Confirm password' -AsSecureString
        if (-not (Test-SecurePasswordMatch $first $second)) {
            Write-Host '  Passwords do not match. Try again.' -ForegroundColor Yellow
            continue
        }
        if (-not (Test-PasswordComplexity $first)) {
            Write-Host '  Password needs 8+ characters and 3 of: lowercase, uppercase, number, symbol.' -ForegroundColor Yellow
            continue
        }
        return $first
    }
}

function Select-Manager {
    $props = @('Id', 'DisplayName', 'UserPrincipalName', 'JobTitle')
    while ($true) {
        $entry = ([string](Read-Host 'Manager (email, or start of their name; blank to skip)')).Trim()
        if (-not $entry) { return $null }

        $safe = $entry.Replace("'", "''")
        if ($entry -like '*@*') {
            $found = @(Get-MgUser -Filter "userPrincipalName eq '$safe' or mail eq '$safe'" -Property $props -ErrorAction SilentlyContinue)
        } else {
            $found = @(Get-MgUser -Filter "startswith(displayName,'$safe')" -Property $props -Top 15 -ErrorAction SilentlyContinue)
        }

        if ($found.Count -eq 0) {
            Write-Host "  No user found for '$entry'. Try again." -ForegroundColor Yellow
            continue
        }

        if ($found.Count -eq 1) {
            $m = $found[0]
            if (Read-YesNo "  Use $($m.DisplayName) <$($m.UserPrincipalName)>?") { return $m }
            continue
        }

        Write-Host '  More than one match:'
        for ($i = 0; $i -lt $found.Count; $i++) {
            $m = $found[$i]
            Write-Host ('   [{0}] {1} <{2}>  {3}' -f ($i + 1), $m.DisplayName, $m.UserPrincipalName, $m.JobTitle)
        }
        $pick = ([string](Read-Host '  Pick a number (blank to search again)')).Trim()
        $n = 0
        if ([int]::TryParse($pick, [ref]$n) -and $n -ge 1 -and $n -le $found.Count) {
            return $found[$n - 1]
        }
    }
}

function Get-AvailableUnitCount {
    param($Sku)
    if (-not $Sku) { return 0 }
    return ($Sku.PrepaidUnits.Enabled - $Sku.ConsumedUnits)
}

function Select-License {
    $skus = @(Get-MgSubscribedSku -All -ErrorAction Stop)
    $e5   = @($skus | Where-Object { $E5SkuPartNumbers -contains $_.SkuPartNumber })

    if ($e5.Count -eq 1) { return $e5[0] }

    if ($e5.Count -gt 1) {
        Write-Host '  More than one E5 license type is in this tenant. Which one should new users get?'
        $choices = $e5
    } else {
        Write-Host '  No E5 license matched the names in Settings. Pick the license to assign:' -ForegroundColor Yellow
        $choices = @($skus | Where-Object { $_.PrepaidUnits.Enabled -gt 0 } | Sort-Object SkuPartNumber)
    }

    for ($i = 0; $i -lt $choices.Count; $i++) {
        $s = $choices[$i]
        Write-Host ('   [{0}] {1}  ({2} available)' -f ($i + 1), $s.SkuPartNumber, (Get-AvailableUnitCount $s))
    }
    while ($true) {
        $pick = ([string](Read-Host '  Number (blank = no license)')).Trim()
        if (-not $pick) { return $null }
        $n = 0
        if ([int]::TryParse($pick, [ref]$n) -and $n -ge 1 -and $n -le $choices.Count) {
            return $choices[$n - 1]
        }
    }
}

function Get-GroupPlan {
    # Looks up each group and decides how the new user can be added to it.
    param([string[]]$Names)
    $props = @('Id', 'DisplayName', 'Mail', 'GroupTypes', 'MailEnabled', 'SecurityEnabled', 'OnPremisesSyncEnabled')

    foreach ($name in $Names) {
        $safe  = $name.Replace("'", "''")
        $found = @(Get-MgGroup -Filter "displayName eq '$safe'" -Property $props -ErrorAction SilentlyContinue)
        $plan  = [pscustomobject]@{ Name = $name; Group = $null; Method = 'Manual'; Note = '' }

        if ($found.Count -eq 0) {
            $plan.Note = 'Group not found - check the name'
        } elseif ($found.Count -gt 1) {
            $plan.Note = "$($found.Count) groups share this name - add manually"
        } else {
            $g = $found[0]
            $plan.Group = $g
            if ($g.GroupTypes -contains 'DynamicMembership') {
                $plan.Method = 'Dynamic'
                $plan.Note   = 'Dynamic group - its membership rule adds users automatically'
            } elseif ($g.OnPremisesSyncEnabled) {
                $plan.Note   = 'Synced from on-prem AD - add the user in Active Directory'
            } elseif ($g.MailEnabled -and ($g.GroupTypes -notcontains 'Unified')) {
                $plan.Method = 'Exchange'
                $plan.Note   = 'Mail-enabled group - added through Exchange Online'
            } else {
                $plan.Method = 'Graph'
                $plan.Note   = 'Ready'
            }
        }
        $plan
    }
}

function Invoke-WithRetry {
    # A brand-new user can take a few seconds to replicate, so "not found" errors are retried.
    param(
        [Parameter(Mandatory)][scriptblock]$Action,
        [int]$MaxAttempts = 5,
        [int]$DelaySeconds = 5
    )
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            return (& $Action)
        } catch {
            $isReplicationDelay = $_.Exception.Message -match 'Request_ResourceNotFound|does not exist|NotFound'
            if (-not $isReplicationDelay -or $attempt -ge $MaxAttempts) { throw }
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Add-StepResult {
    param(
        [System.Collections.Generic.List[object]]$List,
        [string]$Step,
        [string]$Status,
        [string]$Detail = ''
    )
    $List.Add([pscustomobject]@{ Step = $Step; Status = $Status; Detail = $Detail })
}

$script:ExchangeConnected = $false

function Connect-ExchangeIfNeeded {
    if ($script:ExchangeConnected) { return $true }
    if (-not (Get-Module -ListAvailable -Name ExchangeOnlineManagement)) {
        Write-Host '  ExchangeOnlineManagement module is not installed.' -ForegroundColor Yellow
        return $false
    }
    try {
        Import-Module ExchangeOnlineManagement -ErrorAction Stop
        Connect-ExchangeOnline -UserPrincipalName (Get-MgContext).Account -ShowBanner:$false -ErrorAction Stop
        $script:ExchangeConnected = $true
        return $true
    } catch {
        Write-Host "  Couldn't connect to Exchange Online: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    }
}

function Wait-ForMailbox {
    param([string]$Upn, [int]$Minutes)
    $deadline = (Get-Date).AddMinutes($Minutes)
    Write-Host '  Waiting for Exchange to provision the mailbox' -NoNewline
    while ($true) {
        $recipient = $null
        try { $recipient = Get-Recipient -Identity $Upn -ErrorAction Stop } catch { $recipient = $null }  # not provisioned yet
        if ($recipient -and "$($recipient.RecipientTypeDetails)" -eq 'UserMailbox') {
            Write-Host ' ready.' -ForegroundColor Green
            return $true
        }
        if ((Get-Date) -ge $deadline) {
            Write-Host ' timed out.' -ForegroundColor Yellow
            return $false
        }
        Write-Host '.' -NoNewline
        Start-Sleep -Seconds 20
    }
}

# ============================ Create one user =============================

function Invoke-UserOnboarding {
    param($LicenseSku, $GroupPlan)

    # ---------- Collect details ----------
    Write-Section 'New user details'
    $firstName   = Read-Required 'First name'
    $lastName    = Read-Required 'Last name'
    $displayName = "$firstName $lastName"

    $usernamePart = '{0}.{1}' -f (ConvertTo-UsernamePart $firstName), (ConvertTo-UsernamePart $lastName)
    while ($true) {
        if ($usernamePart -notmatch '^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$') {
            $usernamePart = ((Read-Required "  Couldn't build a valid username from that name. Enter the part before @$Domain") -replace '@.*$', '').ToLower()
            continue
        }
        $upn = "$usernamePart@$Domain"
        if (-not (Test-AddressInUse $upn)) { break }
        Write-Host "  $upn is already in use." -ForegroundColor Yellow
        $usernamePart = ((Read-Required "  Enter a different username (the part before @$Domain)") -replace '@.*$', '').ToLower()
    }
    Write-Host "  Username will be $upn" -ForegroundColor Green

    $securePassword = Read-NewPassword
    $jobTitle   = Read-Required 'Job title'
    $department = Read-Required 'Department'
    $manager    = Select-Manager

    # ---------- Review ----------
    $available = 0
    if ($LicenseSku) {
        $current   = Get-MgSubscribedSku -All | Where-Object { $_.SkuId -eq $LicenseSku.SkuId }
        $available = Get-AvailableUnitCount $current
    }

    $managerText = '(none)'
    if ($manager) { $managerText = "$($manager.DisplayName) <$($manager.UserPrincipalName)>" }
    $licenseText = '(none)'
    if ($LicenseSku) { $licenseText = "$($LicenseSku.SkuPartNumber) ($available available)" }
    $signInText = 'Keeps this password'
    if ($ForcePasswordChangeAtFirstSignIn) { $signInText = 'Must change password' }

    Write-Section 'Review'
    $rows = [ordered]@{
        'First name'    = $firstName
        'Last name'     = $lastName
        'Display name'  = $displayName
        'Username'      = $upn
        'Job title'     = $jobTitle
        'Department'    = $department
        'Country'       = "$Country (usage location $UsageLocation)"
        'Manager'       = $managerText
        'License'       = $licenseText
        'First sign-in' = $signInText
    }
    foreach ($key in $rows.Keys) { Write-Host ('  {0,-14}: {1}' -f $key, $rows[$key]) }
    Write-Host '  Groups        :'
    foreach ($item in $GroupPlan) {
        if ($item.Method -eq 'Graph') { Write-Host "    - $($item.Name)" }
        else { Write-Host "    - $($item.Name)  ($($item.Note))" -ForegroundColor DarkYellow }
    }

    if ($LicenseSku -and $available -le 0) {
        Write-Host ''
        Write-Host "  Warning: no $($LicenseSku.SkuPartNumber) licenses are free. The license step will fail unless one is freed up." -ForegroundColor Yellow
    }

    Write-Host ''
    if (-not (Read-YesNo 'Create this user?')) {
        Write-Host 'Cancelled - nothing was created.' -ForegroundColor Yellow
        return
    }

    $results     = [System.Collections.Generic.List[object]]::new()
    $manualSteps = [System.Collections.Generic.List[string]]::new()

    # ---------- 1. Create account ----------
    Write-Section 'Creating user'
    $newUser = $null
    while (-not $newUser) {
        $userParams = @{
            AccountEnabled    = $true
            DisplayName       = $displayName
            GivenName         = $firstName
            Surname           = $lastName
            UserPrincipalName = $upn
            MailNickname      = $usernamePart
            JobTitle          = $jobTitle
            Department        = $department
            Country           = $Country
            UsageLocation     = $UsageLocation
            PasswordProfile   = @{
                Password                      = (ConvertFrom-SecurePassword $securePassword)
                ForceChangePasswordNextSignIn = $ForcePasswordChangeAtFirstSignIn
            }
        }
        try {
            $newUser = New-MgUser @userParams -ErrorAction Stop
        } catch {
            $msg = $_.Exception.Message
            if ($msg -match 'password') {
                Write-Host "  Microsoft rejected the password: $msg" -ForegroundColor Yellow
                $securePassword = Read-NewPassword
            } else {
                Write-Host "  Couldn't create the user: $msg" -ForegroundColor Red
                return
            }
        }
    }
    $securePassword = $null
    $userParams     = $null
    Add-StepResult -List $results -Step 'Create account' -Status 'Done' -Detail $upn

    # ---------- 2. License ----------
    $licenseOk = $false
    if ($LicenseSku) {
        try {
            $null = Invoke-WithRetry -Action {
                Set-MgUserLicense -UserId $newUser.Id -AddLicenses @(@{ SkuId = $LicenseSku.SkuId }) -RemoveLicenses @() -ErrorAction Stop
            }
            $licenseOk = $true
            Add-StepResult -List $results -Step 'License' -Status 'Done' -Detail $LicenseSku.SkuPartNumber
        } catch {
            Add-StepResult -List $results -Step 'License' -Status 'FAILED' -Detail $_.Exception.Message
        }
    } else {
        Add-StepResult -List $results -Step 'License' -Status 'Skipped' -Detail 'No license selected'
    }

    # ---------- 3. Manager ----------
    if ($manager) {
        $managerRef = @{ '@odata.id' = "https://graph.microsoft.com/v1.0/users/$($manager.Id)" }
        try {
            $null = Invoke-WithRetry -Action {
                Set-MgUserManagerByRef -UserId $newUser.Id -BodyParameter $managerRef -ErrorAction Stop
            }
            Add-StepResult -List $results -Step 'Manager' -Status 'Done' -Detail $manager.DisplayName
        } catch {
            Add-StepResult -List $results -Step 'Manager' -Status 'FAILED' -Detail $_.Exception.Message
        }
    } else {
        Add-StepResult -List $results -Step 'Manager' -Status 'Skipped' -Detail 'No manager entered'
    }

    # ---------- 4. Groups (Graph) ----------
    foreach ($item in $GroupPlan) {
        $step = "Group: $($item.Name)"
        if ($item.Method -eq 'Graph') {
            try {
                $null = Invoke-WithRetry -Action {
                    New-MgGroupMember -GroupId $item.Group.Id -DirectoryObjectId $newUser.Id -ErrorAction Stop
                }
                Add-StepResult -List $results -Step $step -Status 'Done' -Detail ''
            } catch {
                if ($_.Exception.Message -match 'already exist') {
                    Add-StepResult -List $results -Step $step -Status 'Done' -Detail 'Already a member'
                } else {
                    Add-StepResult -List $results -Step $step -Status 'FAILED' -Detail $_.Exception.Message
                }
            }
        } elseif ($item.Method -eq 'Dynamic') {
            Add-StepResult -List $results -Step $step -Status 'Skipped' -Detail $item.Note
        } elseif ($item.Method -eq 'Manual') {
            Add-StepResult -List $results -Step $step -Status 'ACTION' -Detail $item.Note
            $manualSteps.Add("Add $upn to '$($item.Name)' - $($item.Note)")
        }
    }

    # ---------- 5. Groups (Exchange Online) ----------
    $mailGroups = @($GroupPlan | Where-Object { $_.Method -eq 'Exchange' })
    if ($mailGroups.Count -gt 0) {
        $ready  = $false
        $reason = ''
        if (-not $licenseOk) {
            $reason = 'User has no mailbox yet (license not assigned)'
        } else {
            Write-Host ''
            Write-Host "  Mail-enabled group(s): $(($mailGroups | ForEach-Object { $_.Name }) -join ', ')" -ForegroundColor Cyan
            Write-Host '  These are updated in Exchange Online, which usually needs a few minutes to see a new mailbox.'
            if (Read-YesNo "  Wait (up to $ExchangeWaitMinutes min) and add now?") {
                if (Connect-ExchangeIfNeeded) {
                    $ready = Wait-ForMailbox -Upn $upn -Minutes $ExchangeWaitMinutes
                    if (-not $ready) { $reason = "Mailbox not ready after $ExchangeWaitMinutes min - run the command below later" }
                } else {
                    $reason = 'Exchange Online not available - run the command below'
                }
            } else {
                $reason = 'Skipped for now - run the command below later'
            }
        }

        foreach ($item in $mailGroups) {
            $step     = "Group: $($item.Name)"
            $identity = $item.Name
            if ($item.Group.Mail) { $identity = $item.Group.Mail }
            $why = $reason
            if ($ready) {
                try {
                    Add-DistributionGroupMember -Identity $identity -Member $upn -BypassSecurityGroupManagerCheck -ErrorAction Stop
                    Add-StepResult -List $results -Step $step -Status 'Done' -Detail 'via Exchange Online'
                    continue
                } catch {
                    if ($_.Exception.Message -match 'already a member') {
                        Add-StepResult -List $results -Step $step -Status 'Done' -Detail 'Already a member'
                        continue
                    }
                    $why = $_.Exception.Message
                }
            }
            Add-StepResult -List $results -Step $step -Status 'ACTION' -Detail $why
            $manualSteps.Add("Add-DistributionGroupMember -Identity '$identity' -Member '$upn' -BypassSecurityGroupManagerCheck")
        }
    }

    # ---------- Summary ----------
    Write-Section "Summary for $displayName"
    foreach ($r in $results) {
        $color = switch ($r.Status) {
            'Done'    { 'Green' }
            'Skipped' { 'DarkGray' }
            'ACTION'  { 'Yellow' }
            default   { 'Red' }
        }
        Write-Host ('  {0,-8} {1}' -f $r.Status, $r.Step) -ForegroundColor $color -NoNewline
        if ($r.Detail) { Write-Host "  - $($r.Detail)" -ForegroundColor DarkGray } else { Write-Host '' }
    }
    if ($manualSteps.Count -gt 0) {
        Write-Host ''
        Write-Host '  Still to do:' -ForegroundColor Yellow
        foreach ($s in $manualSteps) { Write-Host "    $s" }
    }
}

# ================================== Main ==================================

$missing = @($GraphModules | Where-Object { -not (Get-Module -ListAvailable -Name $_) })
if ($missing.Count -gt 0) {
    Write-Host "Missing PowerShell modules: $($missing -join ', ')" -ForegroundColor Red
    Write-Host 'Install them with:  Install-Module Microsoft.Graph -Scope CurrentUser' -ForegroundColor Yellow
    return
}

try {
    foreach ($m in $GraphModules) { Import-Module $m -ErrorAction Stop }
} catch {
    Write-Host "Couldn't load the Microsoft Graph modules: $($_.Exception.Message)" -ForegroundColor Red
    return
}

try {
    $ctx = Get-MgContext
    $needConnect = (-not $ctx) -or (@($GraphScopes | Where-Object { $ctx.Scopes -notcontains $_ }).Count -gt 0)
    if ($needConnect) {
        Write-Host 'Signing in to Microsoft Graph...' -ForegroundColor Cyan
        Connect-MgGraph -Scopes $GraphScopes -NoWelcome -ErrorAction Stop
    }
} catch {
    Write-Host "Couldn't connect to Microsoft Graph: $($_.Exception.Message)" -ForegroundColor Red
    return
}
Write-Host "Connected to Microsoft Graph as $((Get-MgContext).Account)" -ForegroundColor Green

Write-Section 'License'
$licenseSku = $null
try {
    $licenseSku = Select-License
} catch {
    Write-Host "  Couldn't read the tenant's licenses: $($_.Exception.Message)" -ForegroundColor Red
}
if ($licenseSku) {
    Write-Host "  Using $($licenseSku.SkuPartNumber) ($(Get-AvailableUnitCount $licenseSku) available)" -ForegroundColor Green
} else {
    Write-Host '  New users will NOT get a license.' -ForegroundColor Yellow
}

Write-Section 'Onboarding groups'
$groupPlan = @(Get-GroupPlan -Names $OnboardingGroups)
foreach ($item in $groupPlan) {
    $color = switch ($item.Method) {
        'Graph'    { 'Green' }
        'Exchange' { 'Cyan' }
        'Dynamic'  { 'DarkGray' }
        default    { 'Yellow' }
    }
    Write-Host ('  {0,-36} {1}' -f $item.Name, $item.Note) -ForegroundColor $color
}

do {
    Invoke-UserOnboarding -LicenseSku $licenseSku -GroupPlan $groupPlan
} while (Read-YesNo "`nCreate another user?" -Default $false)

if ($script:ExchangeConnected) {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host ''
Write-Host 'Done.' -ForegroundColor Green