#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-ClearAssignedAccessAsSystem {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $taskName = 'Geekom-Kiosk-Clear-AssignedAccess'
    $workerPath = Join-Path $WorkingDirectory 'clear-assigned-access.ps1'
    $resultPath = Join-Path $WorkingDirectory 'clear-result.txt'

    $worker = @'
param([Parameter(Mandatory = $true)][string]$ResultPath)
$ErrorActionPreference = 'Stop'
try {
    $assignedAccess = Get-CimInstance -Namespace 'root\cimv2\mdm\dmmap' -ClassName 'MDM_AssignedAccess'
    $assignedAccess.Configuration = $null
    Set-CimInstance -CimInstance $assignedAccess | Out-Null
    [System.IO.File]::WriteAllText($ResultPath, 'OK')
    exit 0
}
catch {
    [System.IO.File]::WriteAllText($ResultPath, ('ERROR: ' + $_.Exception.Message))
    exit 1
}
'@

    Set-Content -LiteralPath $workerPath -Value $worker -Encoding UTF8
    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

    $powerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -ResultPath "{1}"' -f $workerPath, $resultPath
    $action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $arguments
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable

    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null

    try {
        Start-ScheduledTask -TaskName $taskName
        $deadline = (Get-Date).AddSeconds(90)

        while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $resultPath)) {
            Start-Sleep -Milliseconds 500
        }

        if (-not (Test-Path -LiteralPath $resultPath)) {
            throw 'Zeitueberschreitung beim Entfernen von Assigned Access.'
        }

        $result = (Get-Content -LiteralPath $resultPath -Raw).Trim()
        if ($result -ne 'OK') {
            throw $result
        }
    }
    finally {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    }
}

function Remove-PolicyValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (Test-Path -LiteralPath $Path) {
        Remove-ItemProperty -LiteralPath $Path -Name $Name -Force -ErrorAction SilentlyContinue
    }
}

try {
    $workingDirectory = Join-Path $env:ProgramData 'GeekomKiosk'
    New-Item -ItemType Directory -Path $workingDirectory -Force | Out-Null
    Invoke-ClearAssignedAccessAsSystem -WorkingDirectory $workingDirectory

    $edgePolicyPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
    $edgeUiPolicyPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\EdgeUI'

    foreach ($name in @(
        'KioskSwipeGesturesEnabled', 'PrintingEnabled', 'DeveloperToolsAvailability',
        'RemoteDebuggingAllowed', 'PasswordManagerEnabled', 'BrowserSignin',
        'SyncDisabled', 'BackgroundModeEnabled', 'StartupBoostEnabled',
        'DefaultContextMenuSetting', 'ConfigureKeyboardShortcuts'
    )) {
        Remove-PolicyValue -Path $edgePolicyPath -Name $name
    }

    Remove-PolicyValue -Path $edgeUiPolicyPath -Name 'AllowEdgeSwipe'
    Remove-Item -LiteralPath (Join-Path $edgePolicyPath 'URLBlocklist') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $edgePolicyPath 'URLAllowlist') -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host 'ERFOLG: Kioskzuweisung und die von Skript 08 gesetzten Richtlinien wurden entfernt.' -ForegroundColor Green
    Write-Host 'Naechster Schritt: Neustart mit  shutdown /r /t 0'
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}

