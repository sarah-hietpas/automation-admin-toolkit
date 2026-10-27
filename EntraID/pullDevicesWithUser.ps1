<#
.SYNOPSIS
Exports Microsoft Entra ID devices and their registered owners.

.DESCRIPTION
Connects to Microsoft Graph and searches for Entra ID groups whose
display names contain a specified search string.

Retrieves device members from each matching group and collects device
information including device name, device ID, operating system, join
type, last sign-in date, and registered owner.

Registered owner information is retrieved from the device relationship
and, when necessary, resolved through the associated user account.

Displays the results in the console and exports them to a CSV file.

.REQUIREMENTS
Microsoft.Graph
Microsoft Graph permissions:
  Group.Read.All
  Device.Read.All
  Directory.Read.All
  User.Read.All

.EXAMPLE
.\pullDevicesWithUser.ps1
#>

# Connect to Microsoft Graph
Connect-MgGraph -Scopes "Group.Read.All","Device.Read.All","Directory.Read.All","User.Read.All"

# Search string
$searchString = "searchString"

# Export path
$exportPath = ".\Devices.csv"

# Create exports folder if it does not exist
if (!(Test-Path ".\exports")) {
    New-Item -ItemType Directory -Path ".\exports" | Out-Null
}

# Get all groups that contain the name
$groups = Get-MgGroup -All -Property "id,displayName" | Where-Object {
    $_.DisplayName -like "*$searchString*"
}

$results = @()

foreach ($group in $groups) {

    Write-Host "Processing group: $($group.DisplayName)" -ForegroundColor Cyan

    # Get members of the group
    $members = Get-MgGroupMember -GroupId $group.Id -All

    foreach ($member in $members) {

        # Only process device members
        if ($member.AdditionalProperties["@odata.type"] -eq "#microsoft.graph.device") {

            # This is the Entra object ID of the device
            $entraDeviceObjectId = $member.Id

            if ([string]::IsNullOrWhiteSpace($entraDeviceObjectId)) {
                Write-Warning "Skipping a device member in $($group.DisplayName) because the member ID is blank."
                continue
            }

            try {
                # Get device details
                $device = Get-MgDevice -DeviceId $entraDeviceObjectId -Property `
                    "id,displayName,deviceId,operatingSystem,trustType,approximateLastSignInDateTime" `
                    -ErrorAction Stop
            }
            catch {
                Write-Warning "Could not retrieve device details for object ID $entraDeviceObjectId"
                continue
            }

            # Default blank registered owner
            $registeredOwnerValue = ""

            try {
                # Get registered owner object(s)
                $registeredOwners = Get-MgDeviceRegisteredOwner -DeviceId $entraDeviceObjectId -All -ErrorAction Stop

                $ownerValues = foreach ($owner in $registeredOwners) {

                    # Try AdditionalProperties first
                    if ($null -ne $owner.AdditionalProperties) {

                        if ($owner.AdditionalProperties.ContainsKey("userPrincipalName") -and $owner.AdditionalProperties["userPrincipalName"]) {
                            $owner.AdditionalProperties["userPrincipalName"]
                            continue
                        }

                        if ($owner.AdditionalProperties.ContainsKey("mail") -and $owner.AdditionalProperties["mail"]) {
                            $owner.AdditionalProperties["mail"]
                            continue
                        }

                        if ($owner.AdditionalProperties.ContainsKey("displayName") -and $owner.AdditionalProperties["displayName"]) {
                            $owner.AdditionalProperties["displayName"]
                            continue
                        }
                    }

                    # Fallback: use the owner ID to look up the user directly
                    if (-not [string]::IsNullOrWhiteSpace($owner.Id)) {
                        try {
                            $ownerUser = Get-MgUser -UserId $owner.Id -Property "displayName,userPrincipalName,mail" -ErrorAction Stop

                            if ($ownerUser.UserPrincipalName) {
                                $ownerUser.UserPrincipalName
                            }
                            elseif ($ownerUser.Mail) {
                                $ownerUser.Mail
                            }
                            elseif ($ownerUser.DisplayName) {
                                $ownerUser.DisplayName
                            }
                        }
                        catch {
                            Write-Warning "Could not retrieve registered owner user details for device $($device.DisplayName)"
                        }
                    }
                }

                $registeredOwnerValue = ($ownerValues | Where-Object {
                    -not [string]::IsNullOrWhiteSpace($_)
                }) -join "; "
            }
            catch {
                Write-Warning "Could not retrieve registered owner for device $($device.DisplayName)"
            }

            # Add selected columns only
            $results += [PSCustomObject]@{
                "Group Name"        = $group.DisplayName
                "Device Name"       = $device.DisplayName
                "Device ID"         = $device.DeviceId
                "Operating System"  = $device.OperatingSystem
                "Join Type"         = $device.TrustType
                "Last Sign-In Date" = $device.ApproximateLastSignInDateTime
                "Registered Owner"  = $registeredOwnerValue
            }
        }
    }
}

# Display results
$results | Format-Table -AutoSize

# Export to CSV
$results | Export-Csv $exportPath -NoTypeInformation

Write-Host "`nExport complete: $exportPath" -ForegroundColor Green