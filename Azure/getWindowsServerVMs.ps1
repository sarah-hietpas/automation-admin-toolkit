<#
.SYNOPSIS
Exports an inventory of Azure Windows Server virtual machines.

.DESCRIPTION
Connects to Azure and retrieves virtual machines running Windows Server.
Collects information including VM name, resource group, location,
operating system type, OS SKU, public IP address, and DNS name.

Displays the inventory in the console and exports the results to
WindowsServer_VMs_WithDNS.csv.

.REQUIREMENTS
Az.Accounts
Az.Compute
Az.Network
Permissions to read virtual machines, network interfaces,
and public IP resources

.EXAMPLE
.\getWindowsServerVMs.ps1
#>

# Connect to Azure tenant and subscription
Connect-AzAccount

# Connect to Azure tenant and subscription
# Step 1: Tenant & Subscription Selection
# $tenants = Get-AzTenant
# $selectedTenant = $tenants | Out-GridView -Title "Select a Tenant" -PassThru
# Connect-AzAccount -Tenant $selectedTenant.Id

# $subscriptions = Get-AzSubscription
# $selectedSubscription = $subscriptions | Out-GridView -Title "Select a Subscription" -PassThru
# Set-AzContext -SubscriptionId $selectedSubscription.Id

# Step 2: Get all VMs
$vms = Get-AzVM

# Step 3: Filter for Windows Server VMs
$windowsServerVMs = $vms | Where-Object {
    $_.StorageProfile.OSDisk.OSType -eq 'Windows' -and
    $_.StorageProfile.ImageReference.Offer -eq 'WindowsServer'
}

# Step 4: Build Output with Public IP and DNS
$vmInfo = foreach ($vm in $windowsServerVMs) {
    $nicId = $vm.NetworkProfile.NetworkInterfaces[0].Id
    $nic = Get-AzNetworkInterface -ResourceId $nicId

    $publicIpRef = $nic.IpConfigurations[0].PublicIpAddress
    if ($publicIpRef) {
        # Extract resource group and name from the public IP resource ID
        $publicIpIdParts = $publicIpRef.Id -split "/"
        $publicIpRg = $publicIpIdParts[4]
        $publicIpName = $publicIpIdParts[-1]

        # Get the Public IP resource
        $publicIp = Get-AzPublicIpAddress -Name $publicIpName -ResourceGroupName $publicIpRg
        $publicIpAddress = $publicIp.IpAddress
        $dnsName = $publicIp.DnsSettings.Fqdn
    } else {
        $publicIpAddress = "None"
        $dnsName = "None"
    }

    [PSCustomObject]@{
        Name           = $vm.Name
        ResourceGroup  = $vm.ResourceGroupName
        Location       = $vm.Location
        OSType         = $vm.StorageProfile.OSDisk.OSType
        OSSku          = $vm.StorageProfile.ImageReference.Sku
        PublicIP       = $publicIpAddress
        DNSName        = $dnsName
    }
}

# Step 5: Display the results
$vmInfo | Format-Table -AutoSize

# Optional: Export to CSV
$vmInfo | Export-Csv -Path "WindowsServer_VMs_WithDNS.csv" -NoTypeInformation