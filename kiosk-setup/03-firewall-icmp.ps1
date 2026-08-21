#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$ServerIp
)

Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($ServerIp)) {
    Write-Host "Verwendung: $($MyInvocation.MyCommand.Name) SERVER-IP"
    Write-Host "Beispiel:   $($MyInvocation.MyCommand.Name) 192.168.2.118"
    exit 2
}

$parsedAddress = $null
$isValidIpv4 = [System.Net.IPAddress]::TryParse($ServerIp, [ref]$parsedAddress)

if ($isValidIpv4) {
    $isValidIpv4 = $parsedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork
}

if (-not $isValidIpv4) {
    Write-Error "Ungueltige IPv4-Adresse: $ServerIp"
    exit 2
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = [Security.Principal.WindowsPrincipal]::new($currentIdentity)

if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "PowerShell als Administrator starten und das Skript erneut ausfuehren."
    exit 5
}

$ruleName = "HAWKK-Allow-ICMPv4-Echo-From-Server"

try {
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction Stop

    New-NetFirewallRule `
        -Name $ruleName `
        -DisplayName "HAWKK: ICMPv4-Ping vom Server" `
        -Description "Erlaubt eingehende ICMPv4-Echoanfragen ausschliesslich von $ServerIp." `
        -Enabled True `
        -Profile Any `
        -Direction Inbound `
        -Action Allow `
        -Protocol ICMPv4 `
        -IcmpType 8 `
        -RemoteAddress $parsedAddress.IPAddressToString `
        -ErrorAction Stop |
        Out-Null

    Write-Host "ICMPv4-Firewallregel eingerichtet. Erlaubte Server-IP: $ServerIp"
    exit 0
}
catch {
    Write-Error "ICMPv4-Firewallregel konnte nicht eingerichtet werden: $($_.Exception.Message)"
    exit 1
}
