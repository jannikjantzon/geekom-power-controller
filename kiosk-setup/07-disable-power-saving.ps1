#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

function Invoke-PowerCfg {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & powercfg.exe @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw "powercfg $($Arguments -join ' ') ist fehlgeschlagen (Exit-Code $LASTEXITCODE)."
    }
}

# Wert 0 bedeutet bei diesen Zeitlimits: Nie.
Invoke-PowerCfg -Arguments @("/change", "monitor-timeout-ac", "0")
Invoke-PowerCfg -Arguments @("/change", "monitor-timeout-dc", "0")
Invoke-PowerCfg -Arguments @("/change", "disk-timeout-ac", "0")
Invoke-PowerCfg -Arguments @("/change", "disk-timeout-dc", "0")
Invoke-PowerCfg -Arguments @("/change", "standby-timeout-ac", "0")
Invoke-PowerCfg -Arguments @("/change", "standby-timeout-dc", "0")
Invoke-PowerCfg -Arguments @("/change", "hibernate-timeout-ac", "0")
Invoke-PowerCfg -Arguments @("/change", "hibernate-timeout-dc", "0")

# Deaktiviert Ruhezustand und damit auch Windows Fast Startup.
Invoke-PowerCfg -Arguments @("/hibernate", "off")

Write-Host "OK: Bildschirm, Festplatte, Standby und Ruhezustand stehen auf Nie."
Write-Host "OK: Ruhezustand und Fast Startup sind deaktiviert."
