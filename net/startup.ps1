#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [switch]$Worker
)

$ErrorActionPreference = "Stop"

$TaskName   = "Geekom IPC Connections"
$ConfigPath = Join-Path $PSScriptRoot "config.yml"
$LogFolder  = Join-Path $env:ProgramData "GeekomPower"
$LogPath    = Join-Path $LogFolder "net-use-startup.log"
$NetExe     = Join-Path $env:SystemRoot "System32\net.exe"

function Write-Log {
    param([Parameter(Mandatory)][string]$Message)

    if (-not (Test-Path -LiteralPath $LogFolder)) {
        New-Item -ItemType Directory -Path $LogFolder -Force | Out-Null
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "$timestamp $Message"
}

function ConvertFrom-SimpleYamlValue {
    param(
        [Parameter(Mandatory)][string]$Value,
        [switch]$Password
    )

    $value = $Value.Trim()

    if ($value.StartsWith("'")) {
        if (-not $value.EndsWith("'") -or $value.Length -lt 2) {
            throw "Ungueltiger einfach-quotierter YAML-Wert: $Value"
        }

        return $value.Substring(1, $value.Length - 2).Replace("''", "'")
    }

    if ($value.StartsWith('"')) {
        try {
            return ($value | ConvertFrom-Json)
        }
        catch {
            throw "Ungueltiger doppelt-quotierter YAML-Wert: $Value"
        }
    }

    if ($Password) {
        throw "Passwoerter muessen in config.yml in einfachen oder doppelten Anfuehrungszeichen stehen."
    }

    return (($value -split '\s+#', 2)[0]).Trim()
}

function Read-ConnectionConfig {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Nicht gefunden: $ConfigPath"
    }

    $rawEntries = [System.Collections.Generic.List[object]]::new()
    $current = $null

    foreach ($line in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
        $trimmed = $line.Trim()

        if (-not $trimmed -or $trimmed.StartsWith('#') -or $trimmed -eq 'connections:') {
            continue
        }

        if ($trimmed -match '^-\s+ip:\s*(.+)$') {
            if ($null -ne $current) {
                $rawEntries.Add([pscustomobject]$current)
            }

            $current = [ordered]@{
                Ip       = ConvertFrom-SimpleYamlValue -Value $Matches[1]
                Hostname = $null
                Password = $null
            }
            continue
        }

        if ($null -eq $current) {
            throw "Vor '$trimmed' fehlt ein Listeneintrag mit '- ip: ...'."
        }

        if ($trimmed -match '^hostname:\s*(.+)$') {
            $current.Hostname = ConvertFrom-SimpleYamlValue -Value $Matches[1]
            continue
        }

        if ($trimmed -match '^password:\s*(.*)$') {
            $current.Password = ConvertFrom-SimpleYamlValue -Value $Matches[1] -Password
            continue
        }

        throw "Nicht unterstuetzte Zeile in config.yml: $trimmed"
    }

    if ($null -ne $current) {
        $rawEntries.Add([pscustomobject]$current)
    }

    if ($rawEntries.Count -eq 0) {
        throw "config.yml enthaelt keine Verbindungen."
    }

    $seenIps = @{}
    foreach ($entry in $rawEntries) {
        $parsedIp = $null
        $validIp = [System.Net.IPAddress]::TryParse($entry.Ip, [ref]$parsedIp)

        if (-not $validIp -or $parsedIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
            throw "Ungueltige IPv4-Adresse: $($entry.Ip)"
        }

        if ($seenIps.ContainsKey($entry.Ip)) {
            throw "Doppelte IP-Adresse in config.yml: $($entry.Ip)"
        }
        $seenIps[$entry.Ip] = $true

        if (-not $entry.Hostname -or $entry.Hostname -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,13}[A-Za-z0-9])?$') {
            throw "Ungueltiger Windows-Hostname fuer $($entry.Ip): '$($entry.Hostname)'"
        }

        if ([string]::IsNullOrEmpty($entry.Password)) {
            throw "Fehlendes Passwort fuer $($entry.Ip)."
        }

        if ($entry.Password -match '(?i)(CHANGE_ME|EINTRAGEN|REPLACE)') {
            throw "Das Passwort fuer $($entry.Ip) enthaelt noch einen Platzhalter."
        }
    }

    return @($rawEntries)
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory)][string]$Address,
        [Parameter(Mandatory)][int]$Port,
        [int]$TimeoutMilliseconds = 1000
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    $asyncResult = $null

    try {
        $asyncResult = $client.BeginConnect($Address, $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }

        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $asyncResult) {
            $asyncResult.AsyncWaitHandle.Close()
        }
        $client.Close()
    }
}

function Connect-IpcShare {
    param([Parameter(Mandatory)]$Entry)

    $unc = "\\$($Entry.Ip)\IPC$"
    $remoteUser = "$($Entry.Hostname)\kiosk-power"
    $password = $Entry.Password

    & $NetExe use $unc /delete /y *> $null
    $output = & $NetExe use $unc $password "/user:$remoteUser" /persistent:no 2>&1

    if ($LASTEXITCODE -eq 0) {
        Write-Log "Verbunden: $unc als $remoteUser"
        return $true
    }

    Write-Log "FEHLER bei $unc als $remoteUser`: $($output -join ' ')"
    return $false
}

function Start-Worker {
    $entries = Read-ConnectionConfig
    Write-Log "Worker gestartet; $($entries.Count) Verbindung(en) konfiguriert."

    $states = @{}
    foreach ($entry in $entries) {
        $states[$entry.Ip] = [pscustomobject]@{
            WasOnline = $false
            Connected = $false
        }
    }

    while ($true) {
        foreach ($entry in $entries) {
            $state = $states[$entry.Ip]
            $isOnline = Test-TcpPort -Address $entry.Ip -Port 445

            if (-not $isOnline) {
                if ($state.WasOnline) {
                    Write-Log "Nicht erreichbar: \\$($entry.Ip)\IPC$"
                }
                $state.WasOnline = $false
                $state.Connected = $false
                continue
            }

            # Nur beim Wechsel offline -> online erneut authentifizieren.
            # Dadurch verursacht ein falsches Passwort keine schnelle Login-Schleife.
            if (-not $state.WasOnline) {
                $state.WasOnline = $true
                $state.Connected = Connect-IpcShare -Entry $entry
            }
        }

        Start-Sleep -Seconds 15
    }
}

function Register-StartupTask {
    $entries = Read-ConnectionConfig
    Write-Host "$($entries.Count) Verbindung(en) aus config.yml validiert."

    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $credential = Get-Credential `
        -UserName $currentUser `
        -Message "Windows-Kennwort fuer $currentUser eingeben (nicht die Windows-Hello-PIN)."
    $taskPassword = $credential.GetNetworkCredential().Password

    & icacls.exe $ConfigPath /inheritance:r /grant:r `
        "$($credential.UserName):(M)" `
        "SYSTEM:(F)" `
        "*S-1-5-32-544:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Die Dateirechte von config.yml konnten nicht abgesichert werden."
    }

    $powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Worker"

    $action = New-ScheduledTaskAction `
        -Execute $powerShellExe `
        -Argument $arguments `
        -WorkingDirectory $PSScriptRoot

    $trigger = New-ScheduledTaskTrigger -AtStartup

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $credential.UserName `
        -Password $taskPassword `
        -RunLevel Highest `
        -Force | Out-Null

    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2

    $registeredTask = Get-ScheduledTask -TaskName $TaskName
    if ($registeredTask.State -ne 'Running') {
        throw "Der Task wurde registriert, laeuft aber nicht. Pruefe $LogPath."
    }

    $registeredTask | Select-Object TaskName, State
}

if ($Worker) {
    Start-Worker
}
else {
    Register-StartupTask
}
