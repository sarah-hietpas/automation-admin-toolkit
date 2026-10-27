<#
.SYNOPSIS
Moves Microsoft Entra ID users from one group to another.

.DESCRIPTION
Connects to Microsoft Graph and processes users listed in a CSV file.
For each user, verifies their account and group memberships, removes
them from the configured source group, and adds them to the configured
destination group.

Users who are not members of the source group can still be added to the
destination group. Users who are already members of the destination
group are skipped without error.

Exports a results log containing each user's removal status, add status,
overall result, and any errors encountered during processing.

.REQUIREMENTS
Microsoft.Graph.Authentication
Microsoft.Graph.Users
Microsoft.Graph.Groups
Microsoft Graph permissions:
  User.Read.All
  Group.Read.All
  GroupMember.ReadWrite.All
CSV file containing a "UserPrincipalName" column
Permissions to modify membership of the specified Entra ID groups

.EXAMPLE
.\deleteUsersFromGroupAndMoveToAnotherGroup.ps1
#>

# ============================================================
# Move Entra ID Users From One Group to Another
# ============================================================

# -----------------------------
# CONFIGURATION
# -----------------------------

# Object ID of the group users are being REMOVED from
$SourceGroupId = "SourceGroupId"

# Object ID of the group users are being ADDED to
$DestinationGroupId = "DestinationGroupId"

# Path to the input CSV
$CsvPath = "./UserBatch01.csv"

# Path for the results log
$LogPath = "./Entra_Group_Move_Results.csv"


# -----------------------------
# CONNECT TO MICROSOFT GRAPH
# -----------------------------

# Install Microsoft Graph module if needed:
# Install-Module Microsoft.Graph -Scope CurrentUser

Import-Module Microsoft.Graph.Users
Import-Module Microsoft.Graph.Groups

Connect-MgGraph -Scopes `
    "User.Read.All",
    "Group.Read.All",
    "GroupMember.ReadWrite.All"

Write-Host ""
Write-Host "Connected to Microsoft Graph." -ForegroundColor Green
Write-Host ""


# -----------------------------
# VERIFY GROUPS
# -----------------------------

try {

    $SourceGroup = Get-MgGroup -GroupId $SourceGroupId -ErrorAction Stop
    $DestinationGroup = Get-MgGroup -GroupId $DestinationGroupId -ErrorAction Stop

    Write-Host "Source Group:" -ForegroundColor Cyan
    Write-Host "  $($SourceGroup.DisplayName)"
    Write-Host "  $SourceGroupId"

    Write-Host ""

    Write-Host "Destination Group:" -ForegroundColor Cyan
    Write-Host "  $($DestinationGroup.DisplayName)"
    Write-Host "  $DestinationGroupId"

    Write-Host ""

}
catch {

    Write-Host "Unable to retrieve one or both groups." -ForegroundColor Red
    Write-Host $_.Exception.Message
    return

}


# -----------------------------
# IMPORT USERS
# -----------------------------

if (-not (Test-Path $CsvPath)) {

    Write-Host "CSV file not found:" -ForegroundColor Red
    Write-Host $CsvPath
    return

}

$Users = Import-Csv $CsvPath

if (-not $Users.UserPrincipalName) {

    Write-Host "The CSV must contain a column named UserPrincipalName." -ForegroundColor Red
    return

}

Write-Host "Users loaded from CSV: $($Users.Count)" -ForegroundColor Cyan
Write-Host ""


# -----------------------------
# RESULTS ARRAY
# -----------------------------

$Results = @()


# -----------------------------
# PROCESS USERS
# -----------------------------

$CurrentUserNumber = 0

foreach ($Entry in $Users) {

    $CurrentUserNumber++

    $UPN = $Entry.UserPrincipalName.Trim()

    Write-Host "[$CurrentUserNumber/$($Users.Count)] Processing $UPN" -ForegroundColor Yellow

    $UserId = $null
    $RemoveStatus = "Not Attempted"
    $AddStatus = "Not Attempted"
    $OverallStatus = "Pending"
    $ErrorMessage = ""

    # ---------------------------------
    # FIND USER
    # ---------------------------------

    try {

        $User = Get-MgUser `
            -UserId $UPN `
            -Property Id,DisplayName,UserPrincipalName `
            -ErrorAction Stop

        $UserId = $User.Id

        Write-Host "   Found: $($User.DisplayName)" -ForegroundColor Gray

    }
    catch {

        Write-Host "   User not found." -ForegroundColor Red

        $Results += [PSCustomObject]@{
            UserPrincipalName = $UPN
            DisplayName       = ""
            UserId            = ""
            SourceGroup       = $SourceGroup.DisplayName
            DestinationGroup  = $DestinationGroup.DisplayName
            RemoveStatus      = "User Not Found"
            AddStatus         = "User Not Found"
            OverallStatus     = "Failed"
            Error             = $_.Exception.Message
        }

        continue

    }


    # ---------------------------------
    # CHECK SOURCE GROUP MEMBERSHIP
    # ---------------------------------

    try {

        $SourceMembership = Get-MgGroupMember `
            -GroupId $SourceGroupId `
            -All |
            Where-Object { $_.Id -eq $UserId }

        if ($SourceMembership) {

            Write-Host "   Member of source group." -ForegroundColor Gray

            # -----------------------------
            # REMOVE FROM SOURCE GROUP
            # -----------------------------

            try {

                Remove-MgGroupMemberByRef `
                    -GroupId $SourceGroupId `
                    -DirectoryObjectId $UserId `
                    -ErrorAction Stop

                $RemoveStatus = "Removed"

                Write-Host "   Removed from: $($SourceGroup.DisplayName)" -ForegroundColor Green

            }
            catch {

                $RemoveStatus = "Removal Failed"
                $ErrorMessage += "Remove Error: $($_.Exception.Message); "

                Write-Host "   Failed to remove from source group." -ForegroundColor Red

            }

        }
        else {

            $RemoveStatus = "Not a Member"

            Write-Host "   User is not currently in the source group." -ForegroundColor DarkYellow

        }

    }
    catch {

        $RemoveStatus = "Membership Check Failed"
        $ErrorMessage += "Source Membership Check Error: $($_.Exception.Message); "

        Write-Host "   Unable to check source group membership." -ForegroundColor Red

    }


    # ---------------------------------
    # CHECK DESTINATION MEMBERSHIP
    # ---------------------------------

    try {

        $DestinationMembership = Get-MgGroupMember `
            -GroupId $DestinationGroupId `
            -All |
            Where-Object { $_.Id -eq $UserId }

        if ($DestinationMembership) {

            $AddStatus = "Already a Member"

            Write-Host "   Already in: $($DestinationGroup.DisplayName)" -ForegroundColor DarkYellow

        }
        else {

            # -----------------------------
            # ADD TO DESTINATION GROUP
            # -----------------------------

            try {

                New-MgGroupMember `
                    -GroupId $DestinationGroupId `
                    -DirectoryObjectId $UserId `
                    -ErrorAction Stop

                $AddStatus = "Added"

                Write-Host "   Added to: $($DestinationGroup.DisplayName)" -ForegroundColor Green

            }
            catch {

                $AddStatus = "Add Failed"
                $ErrorMessage += "Add Error: $($_.Exception.Message); "

                Write-Host "   Failed to add to destination group." -ForegroundColor Red

            }

        }

    }
    catch {

        $AddStatus = "Membership Check Failed"
        $ErrorMessage += "Destination Membership Check Error: $($_.Exception.Message); "

        Write-Host "   Unable to check destination group membership." -ForegroundColor Red

    }


    # ---------------------------------
    # DETERMINE FINAL STATUS
    # ---------------------------------

    if (
        ($RemoveStatus -eq "Removed" -or $RemoveStatus -eq "Not a Member") -and
        ($AddStatus -eq "Added" -or $AddStatus -eq "Already a Member")
    ) {

        $OverallStatus = "Success"

    }
    else {

        $OverallStatus = "Failed"

    }


    # ---------------------------------
    # ADD TO LOG
    # ---------------------------------

    $Results += [PSCustomObject]@{

        UserPrincipalName = $User.UserPrincipalName
        DisplayName       = $User.DisplayName
        UserId            = $User.Id

        SourceGroup       = $SourceGroup.DisplayName
        DestinationGroup  = $DestinationGroup.DisplayName

        RemoveStatus      = $RemoveStatus
        AddStatus         = $AddStatus
        OverallStatus     = $OverallStatus

        Error             = $ErrorMessage

    }

    Write-Host ""

}


# -----------------------------
# EXPORT RESULTS
# -----------------------------

$Results |
    Export-Csv `
        -Path $LogPath `
        -NoTypeInformation `
        -Encoding UTF8


# -----------------------------
# SUMMARY
# -----------------------------

$Successful = ($Results | Where-Object OverallStatus -eq "Success").Count
$Failed = ($Results | Where-Object OverallStatus -eq "Failed").Count

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Migration Complete"
Write-Host "=============================================" -ForegroundColor Cyan

Write-Host "Total users:  $($Results.Count)"
Write-Host "Successful:   $Successful" -ForegroundColor Green
Write-Host "Failed:       $Failed" -ForegroundColor Red

Write-Host ""
Write-Host "Results exported to:"
Write-Host $LogPath -ForegroundColor Cyan

Write-Host ""

Disconnect-MgGraph