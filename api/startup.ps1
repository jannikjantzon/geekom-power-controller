#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$TaskName = "Geekom Power API"
)

$ErrorActionPreference = "Stop"

$ApiScript       = Join-Path $PSScriptRoot "power_api.py"
$ControllerScript = Join-Path $PSScriptRoot "powerctl.py"
$ConfigPath      = Join-Path $PSScriptRoot "config.json"
$RequirementsPath = Join-Path $PSScriptRoot "requirements.txt"

foreach ($requiredFile in @($ApiScript, $ControllerScript, $ConfigPath, $RequirementsPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Datei fehlt: $requiredFile"
    }
}

$PythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source

# Nur beim ersten Lauf und nur falls erforderlich werden Flask/Waitress installiert.
& $PythonLauncher -3 -c "import flask, waitress" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Flask/Waitress fehlen und werden fuer den aktuellen Windows-Benutzer installiert."
    & $PythonLauncher -3 -m pip install --user --disable-pip-version-check -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Flask/Waitress konnten nicht installiert werden."
    }
}

& $PythonLauncher -3 $ApiScript --config $ConfigPath --check
if ($LASTEXITCODE -ne 0) {
    throw "config.json oder Controller-Konfiguration ist ungueltig."
}

$configuration = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$apiPort = [int]$configuration.api.port

$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Credential = Get-Credential `
    -UserName $CurrentUser `
    -Message "Windows-Kennwort fuer $CurrentUser eingeben (nicht die Windows-Hello-PIN)."
$TaskPassword = $Credential.GetNetworkCredential().Password

& icacls.exe $ConfigPath /inheritance:r /grant:r `
    "$($Credential.UserName):(M)" `
    "SYSTEM:(F)" `
    "*S-1-5-32-544:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Die Dateirechte von config.json konnten nicht abgesichert werden."
}

$Arguments = "-3 `"$ApiScript`" --config `"$ConfigPath`""
$Action = New-ScheduledTaskAction `
    -Execute $PythonLauncher `
    -Argument $Arguments `
    -WorkingDirectory $PSScriptRoot

$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $Credential.UserName `
    -Password $TaskPassword `
    -RunLevel Highest `
    -Description "Authenticated HTTP API for the GEEKOM power controller" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

$Deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    try {
        $Health = Invoke-RestMethod `
            -Method Get `
            -Uri "http://127.0.0.1:$apiPort/healthz" `
            -TimeoutSec 2
    }
    catch {
        $Health = $null
    }
} until (($null -ne $Health -and $Health.ok) -or (Get-Date) -ge $Deadline)

if ($null -eq $Health -or -not $Health.ok) {
    throw "Task wurde registriert, aber die API antwortet nicht auf Port $apiPort. Pruefe power-api.log."
}

$RegisteredTask = Get-ScheduledTask -TaskName $TaskName
if ($RegisteredTask.State -ne 'Running') {
    throw "Die API antwortet, aber der registrierte Task laeuft nicht. Moeglicherweise belegt ein anderer Prozess Port $apiPort."
}

Write-Host "OK: API laeuft und startet kuenftig automatisch."
$RegisteredTask | Select-Object TaskName, State
