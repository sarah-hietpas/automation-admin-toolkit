<#
.SYNOPSIS
Creates Azure managed disk snapshots for virtual machines.

.DESCRIPTION
Connects to Azure and processes virtual machines listed in a CSV file.
Retrieves each VM's operating system and data disks and creates snapshots
of those disks in a designated Azure resource group and region.

Snapshots are tagged with the creation date and creator information.

.REQUIREMENTS
Az.Accounts
Az.Compute
CSV file containing "VMName" and "ResourceGroup" columns
Existing target resource group
Permissions to read managed disks and create snapshots

.EXAMPLE
.\createSnapshotVMDisk.ps1
#>

# Connect and select subscription
Connect-AzAccount
$subscriptionId = "your-subscription-id"
Select-AzSubscription -SubscriptionId $subscriptionId

# Target backup resource group and location
$targetRg = "TargetRG"
$targetLocation = "northeurope"

# Today's date
$dateShort = Get-Date -Format "yyyyMMdd"
$dateTag = Get-Date -Format "yyyy-MM-dd"

# Import VMs from CSV
$vmList = Import-Csv -Path ".\vm_list.csv"

foreach ($vm in $vmList) {
    $vmName = $vm.VMName
    $sourceRg = $vm.ResourceGroup

    Write-Output "Processing VM: $vmName in RG: $sourceRg"
    $vmObject = Get-AzVM -Name $vmName -ResourceGroupName $sourceRg

    $allDisks = @($vmObject.StorageProfile.OsDisk) + $vmObject.StorageProfile.DataDisks

    foreach ($diskRef in $allDisks) {
        $disk = Get-AzDisk -ResourceGroupName $sourceRg -DiskName $diskRef.Name

        $snapshotName = "${vmName}_snapshot_${dateShort}"

        $snapshotConfig = New-AzSnapshotConfig `
            -SourceUri $disk.Id `
            -Location $targetLocation `
            -CreateOption Copy `
            -SkuName Standard_ZRS  # Standard HDD, zone-redundant

        Write-Output "Creating snapshot: $snapshotName for disk: $($disk.Name)"

        New-AzSnapshot `
            -Snapshot $snapshotConfig `
            -SnapshotName $snapshotName `
            -ResourceGroupName $targetRg `
            -Tag @{ "CreatedBy" = "Sarah Centeno"; "Date" = $dateTag }
    }
}
