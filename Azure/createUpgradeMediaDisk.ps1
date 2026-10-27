<#
.SYNOPSIS
Creates an Azure Windows Server upgrade media managed disk.

.DESCRIPTION
Connects to Azure and retrieves the latest Windows Server upgrade image
from the Azure Marketplace for the configured server version.

Creates the target resource group if necessary and provisions a managed
disk from the upgrade image. The disk can then be attached to an Azure
virtual machine to perform an in-place Windows Server upgrade.

.REQUIREMENTS
Az.Accounts
Az.Compute
Az.Resources
Permissions to create Azure managed disks and resource groups
Access to the Windows Server Upgrade Marketplace image

.EXAMPLE
.\createUpgradeMediaDisk.ps1
#>

Connect-AzAccount
# Set-AzContext -Subscription '12f698a4-344d-4258-aa0f-40acbcc93aa4'

# Customer specific parameters

# Resource group of the source VM
$resourceGroup = "RG"

# Location of the source VM
$location = "NorthEurope"

# Zone of the source VM, if any
# $zone = ""

# Disk name for the that will be created
$diskName = "WindowsServer2025UpgradeDisk"

# Target version for the upgrade - must be either server2022Upgrade, server2019Upgrade, server2016Upgrade or server2012Upgrade
$sku = "server2025Upgrade"

# Common parameters

$publisher = "MicrosoftWindowsServer"
$offer = "WindowsServerUpgrade"
$managedDiskSKU = "Standard_LRS"

#
# Get the latest version of the special (hidden) VM Image from the Azure Marketplace

$versions = Get-AzVMImage -PublisherName $publisher -Location $location -Offer $offer -Skus $sku | sort-object -Descending {[version] $_.Version	}
$latestString = $versions[0].Version

# Get the special (hidden) VM Image from the Azure Marketplace by version - the image is used to create a disk to upgrade to the new version

$image = Get-AzVMImage -Location $location -PublisherName $publisher -Offer $offer -Skus $sku -Version $latestString

#
# Create Resource Group if it doesn't exist
#

if (-not (Get-AzResourceGroup -Name $resourceGroup -ErrorAction SilentlyContinue)) {
    New-AzResourceGroup -Name $resourceGroup -Location $location    
}

#
# Create Managed Disk from LUN 0
#

if ($zone){
    $diskConfig = New-AzDiskConfig -SkuName $managedDiskSKU -CreateOption FromImage -Zone $zone -Location $location
} else {
    $diskConfig = New-AzDiskConfig -SkuName $managedDiskSKU -CreateOption FromImage -Location $location
}

Set-AzDiskImageReference -Disk $diskConfig -Id $image.Id -Lun 0

New-AzDisk -ResourceGroupName $resourceGroup -DiskName $diskName -Disk $diskConfig