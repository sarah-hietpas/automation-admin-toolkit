<#
.SYNOPSIS
Checks SSL certificates for a list of domains.

.DESCRIPTION
Connects to each domain listed in a CSV file over HTTPS port 443
and retrieves its SSL certificate information. Reports the certificate
subject, issuer, thumbprint, validity dates, remaining days until
expiration, and connection status.

Exports the results to ssl-cert-results.csv and displays them
in the console.

.REQUIREMENTS
PowerShell 5.1 or PowerShell 7+
Network access to TCP port 443
CSV file containing a "domain" column

.EXAMPLE
.\CheckSSLSites.ps1
#>

Write-Host "Starting SSL certificate checks..." -ForegroundColor Cyan

# Change file path of CSV file containing list of sites
$Sites = Import-Csv ".\nameOfFile.csv"

$Results = foreach ($Site in $Sites) {

    $Domain = $Site.domain

    Write-Host ""
    Write-Host "=========================================" -ForegroundColor DarkGray
    Write-Host "Checking domain: $Domain" -ForegroundColor Yellow
    Write-Host "=========================================" -ForegroundColor DarkGray

    try {
        Write-Host "Connecting to $Domain on port 443..." -ForegroundColor Gray

        $TcpClient = New-Object Net.Sockets.TcpClient($Domain, 443)

        Write-Host "Creating SSL session..." -ForegroundColor Gray

        $SslStream = New-Object Net.Security.SslStream(
            $TcpClient.GetStream(),
            $false,
            ({ $true })
        )

        $SslStream.AuthenticateAsClient($Domain)

        Write-Host "Retrieving certificate..." -ForegroundColor Gray

        $Cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
            $SslStream.RemoteCertificate
        )

        Write-Host "SUCCESS: Certificate retrieved for $Domain" -ForegroundColor Green
        Write-Host "Expires: $($Cert.NotAfter)" -ForegroundColor Green

        $SslStream.Close()
        $TcpClient.Close()

        [PSCustomObject]@{
            Domain      = $Domain
            Subject     = $Cert.Subject
            Issuer      = $Cert.Issuer
            Thumbprint  = $Cert.Thumbprint
            NotBefore   = $Cert.NotBefore
            NotAfter    = $Cert.NotAfter
            DaysLeft    = ($Cert.NotAfter - (Get-Date)).Days
            Status      = "OK"
        }
    }
    catch {

        Write-Host "FAILED: Could not retrieve certificate for $Domain" -ForegroundColor Red
        Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Red

        [PSCustomObject]@{
            Domain      = $Domain
            Subject     = ""
            Issuer      = ""
            Thumbprint  = ""
            NotBefore   = ""
            NotAfter    = ""
            DaysLeft    = ""
            Status      = "Failed: $($_.Exception.Message)"
        }
    }
    finally {
        Write-Host "Finished processing: $Domain" -ForegroundColor Cyan
    }
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor DarkGray
Write-Host "Exporting results to ssl-cert-results.csv" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor DarkGray

$Results | Export-Csv ".\ssl-cert-results.csv" -NoTypeInformation

Write-Host ""
Write-Host "SSL certificate check completed." -ForegroundColor Green
Write-Host "Results saved to ssl-cert-results.csv" -ForegroundColor Green

$Results | Format-Table -AutoSize