<#
.SYNOPSIS
Exports members of matching Microsoft Entra ID groups.

.DESCRIPTION
Connects to Microsoft Graph and searches for Entra ID groups whose
display names contain a specified search string.

Retrieves the direct members of each matching group and, when the
member is a user, retrieves the user's display name and user principal
name.

Displays the results in the console and exports group and member
information to a CSV file.

.REQUIREMENTS
Microsoft.Graph
Microsoft Graph permissions:
  Group.Read.All
  User.Read.All
An existing output directory for the CSV export

.EXAMPLE
.\pullGroupMemebers.ps1
#>

Install-Module Microsoft.Graph -Scope CurrentUser -Repository PSGallery -Force
Import-Module Microsoft.Graph

# Connect to Microsoft Graph
Connect-MgGraph -Scopes "Group.Read.All","User.Read.All"

# Search string
$searchString = "searchString"

# Get all groups that contain the name
$groups = Get-MgGroup -All | Where-Object {
    $_.DisplayName -like "*$searchString*"
}

$results = @()

foreach ($group in $groups) {

    Write-Host "Processing group: $($group.DisplayName)" -ForegroundColor Cyan

    # Get members of the group
    $members = Get-MgGroupMember -GroupId $group.Id -All

    foreach ($member in $members) {

        # Try to get user details (if member is a user)
        $user = $null
        if ($member.AdditionalProperties["@odata.type"] -eq "#microsoft.graph.user") {
            $user = Get-MgUser -UserId $member.Id
        }

        $results += [PSCustomObject]@{
            GroupName       = $group.DisplayName
            GroupId         = $group.Id
            MemberName      = $user.DisplayName
            MemberUPN       = $user.UserPrincipalName
            MemberId        = $member.Id
            MemberType      = $member.AdditionalProperties["@odata.type"]
        }
    }
}

# Display results
$results | Format-Table -AutoSize

# Optional: Export to CSV
$results | Export-Csv ".\exports\GroupMembers.csv" -NoTypeInformation

Write-Host "`nExport complete" -ForegroundColor Green
