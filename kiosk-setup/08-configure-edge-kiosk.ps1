#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Url,

    [Parameter(Mandatory = $false)]
    [string[]]$AdditionalAllowedUrl = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-HttpUri {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $parsed = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed)) {
        throw "Ungueltige URL: $Value"
    }

    if ($parsed.Scheme -notin @('http', 'https')) {
        throw "Nur http:// oder https:// sind erlaubt: $Value"
    }

    return $parsed
}

function Set-DwordPolicy {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Value
    )

    New-Item -Path $Path -Force | Out-Null
    New-ItemProperty -Path $Path -Name $Name -PropertyType DWord -Value $Value -Force | Out-Null
}

function Set-StringPolicy {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    New-Item -Path $Path -Force | Out-Null
    New-ItemProperty -Path $Path -Name $Name -PropertyType String -Value $Value -Force | Out-Null
}

function Invoke-AssignedAccessAsSystem {
    param(
        [Parameter(Mandatory = $false)][AllowNull()][string]$Configuration,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $taskName = 'Geekom-Kiosk-Apply-AssignedAccess'
    $configurationPath = Join-Path $WorkingDirectory 'assigned-access.xml'
    $workerPath = Join-Path $WorkingDirectory 'apply-assigned-access.ps1'
    $resultPath = Join-Path $WorkingDirectory 'apply-result.txt'

    if ($null -eq $Configuration) {
        Set-Content -LiteralPath $configurationPath -Value '' -Encoding UTF8
        $operation = 'clear'
    }
    else {
        Set-Content -LiteralPath $configurationPath -Value $Configuration -Encoding UTF8
        $operation = 'set'
    }

    $worker = @'
param(
    [Parameter(Mandatory = $true)][string]$Operation,
    [Parameter(Mandatory = $true)][string]$ConfigurationPath,
    [Parameter(Mandatory = $true)][string]$ResultPath
)

$ErrorActionPreference = 'Stop'

try {
    $namespaceName = 'root\cimv2\mdm\dmmap'
    $className = 'MDM_AssignedAccess'
    $assignedAccess = Get-CimInstance -Namespace $namespaceName -ClassName $className

    if ($Operation -eq 'clear') {
        $assignedAccess.Configuration = $null
    }
    else {
        $xml = [System.IO.File]::ReadAllText($ConfigurationPath)
        $assignedAccess.Configuration = [System.Net.WebUtility]::HtmlEncode($xml)
    }

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
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Operation "{1}" -ConfigurationPath "{2}" -ResultPath "{3}"' -f `
        $workerPath, $operation, $configurationPath, $resultPath

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
            throw 'Zeitueberschreitung beim Anwenden von Assigned Access.'
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

try {
    $mainUri = Get-HttpUri -Value $Url
    $normalisedUrl = $mainUri.AbsoluteUri

    $allowedPatterns = [System.Collections.Generic.List[string]]::new()
    $allowedPatterns.Add(('{0}://{1}/*' -f $mainUri.Scheme, $mainUri.Authority))

    foreach ($item in $AdditionalAllowedUrl) {
        $additionalUri = Get-HttpUri -Value $item
        $pattern = '{0}://{1}/*' -f $additionalUri.Scheme, $additionalUri.Authority
        if (-not $allowedPatterns.Contains($pattern)) {
            $allowedPatterns.Add($pattern)
        }
    }

    # Die aeussere Array-Klammer ist fuer Windows PowerShell 5.1 notwendig:
    # Bei genau einem Treffer wuerde die Pipeline sonst einen einzelnen String
    # liefern. Unter StrictMode besitzt dieser keine verwendbare Count-Eigenschaft.
    $edgeCandidates = @(
        @(
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
            (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    )

    if ($edgeCandidates.Count -eq 0) {
        throw 'Microsoft Edge wurde nicht gefunden.'
    }

    $edgePath = $edgeCandidates[0]
    $edgePolicyPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
    $edgeUiPolicyPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\EdgeUI'
    $urlBlocklistPath = Join-Path $edgePolicyPath 'URLBlocklist'
    $urlAllowlistPath = Join-Path $edgePolicyPath 'URLAllowlist'

    # Windows-Randgesten und Edge-Wischgesten abschalten.
    Set-DwordPolicy -Path $edgeUiPolicyPath -Name 'AllowEdgeSwipe' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'KioskSwipeGesturesEnabled' -Value 0

    # Browserfunktionen abschalten, die in einem dedizierten Kiosk nicht benoetigt werden.
    Set-DwordPolicy -Path $edgePolicyPath -Name 'PrintingEnabled' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'DeveloperToolsAvailability' -Value 2
    Set-DwordPolicy -Path $edgePolicyPath -Name 'RemoteDebuggingAllowed' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'PasswordManagerEnabled' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'BrowserSignin' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'SyncDisabled' -Value 1
    Set-DwordPolicy -Path $edgePolicyPath -Name 'BackgroundModeEnabled' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'StartupBoostEnabled' -Value 0
    Set-DwordPolicy -Path $edgePolicyPath -Name 'DefaultContextMenuSetting' -Value 2

    # Nur die angegebene Website und optional zusaetzliche Origins duerfen als Seite geoeffnet werden.
    New-Item -Path $urlBlocklistPath -Force | Out-Null
    New-Item -Path $urlAllowlistPath -Force | Out-Null
    New-ItemProperty -Path $urlBlocklistPath -Name '1' -PropertyType String -Value '*' -Force | Out-Null

    $allowIndex = 1
    foreach ($pattern in $allowedPatterns) {
        New-ItemProperty -Path $urlAllowlistPath -Name ([string]$allowIndex) -PropertyType String -Value $pattern -Force | Out-Null
        $allowIndex++
    }

    # Relevante Edge-Tastaturbefehle sperren, einschliesslich Browser-Zoom.
    $disabledCommands = @(
        'back', 'caret_browsing_toggle', 'clear_browsing_data', 'close_find_or_stop',
        'close_tab', 'close_window', 'collections', 'dev_tools', 'dev_tools_console',
        'forward', 'fullscreen', 'help_page', 'history', 'home',
        'immersive_reader_toggle', 'new_application_guard_window', 'new_inprivate_window',
        'new_tab', 'new_window', 'open_file', 'paste_and_go', 'print', 'profile',
        'read_aloud_toggle', 'refresh', 'refresh_bypassing_cache', 'reopen_tab',
        'save_page', 'select_last_tab', 'select_next_tab', 'select_previous_tab',
        'select_tab_0', 'select_tab_1', 'select_tab_2', 'select_tab_3',
        'select_tab_4', 'select_tab_5', 'select_tab_6', 'select_tab_7',
        'send_feedback', 'settings_and_more_menu', 'show_favorites_bar_toggle',
        'sidebar_search_selected_text', 'system_print', 'task_manager',
        'vertical_tabs_toggle', 'view_source', 'web_capture', 'web_select',
        'zoom_in', 'zoom_out', 'zoom_reset'
    )
    $shortcutPolicy = @{ disabled = $disabledCommands } | ConvertTo-Json -Compress
    Set-StringPolicy -Path $edgePolicyPath -Name 'ConfigureKeyboardShortcuts' -Value $shortcutPolicy

    $workingDirectory = Join-Path $env:ProgramData 'GeekomKiosk'
    New-Item -ItemType Directory -Path $workingDirectory -Force | Out-Null

    # 1440 Minuten = Neustart der Kiosksitzung nach 24 Stunden ohne Eingabe.
    # Assigned Access startet Edge ausserdem sofort neu, falls der Prozess beendet wird.
    # --disable-pinch ist eine zusaetzliche Chromium-Sperre; fuer garantierte Touch-Zoom-Sperre
    # muss die Website selbst einen passenden viewport/touch-action setzen.
    $edgeArguments = '--kiosk "{0}" --edge-kiosk-type=fullscreen --kiosk-idle-timeout-minutes=1440 --no-first-run --disable-pinch' -f $normalisedUrl
    $escapedEdgePath = [System.Security.SecurityElement]::Escape($edgePath)
    $escapedEdgeArguments = [System.Security.SecurityElement]::Escape($edgeArguments)
    $profileId = '{' + ([Guid]::NewGuid().ToString().ToUpperInvariant()) + '}'

    $assignedAccessConfiguration = @"
<?xml version="1.0" encoding="utf-8"?>
<AssignedAccessConfiguration
    xmlns="http://schemas.microsoft.com/AssignedAccess/2017/config"
    xmlns:rs5="http://schemas.microsoft.com/AssignedAccess/201810/config"
    xmlns:v4="http://schemas.microsoft.com/AssignedAccess/2021/config">
  <Profiles>
    <Profile Id="$profileId">
      <KioskModeApp
          v4:ClassicAppPath="$escapedEdgePath"
          v4:ClassicAppArguments="$escapedEdgeArguments" />
      <v4:BreakoutSequence Key="Ctrl+Alt+Shift+K" />
    </Profile>
  </Profiles>
  <Configs>
    <Config>
      <AutoLogonAccount rs5:DisplayName="kiosk" />
      <DefaultProfile Id="$profileId" />
    </Config>
  </Configs>
</AssignedAccessConfiguration>
"@

    Invoke-AssignedAccessAsSystem -Configuration $assignedAccessConfiguration -WorkingDirectory $workingDirectory

    Write-Host ''
    Write-Host 'ERFOLG: Der Edge-Kiosk wurde eingerichtet.' -ForegroundColor Green
    Write-Host "Startseite: $normalisedUrl"
    Write-Host ('Erlaubte Website-Bereiche: ' + ($allowedPatterns -join ', '))
    Write-Host 'Kiosk-Konto: wird von Windows automatisch als Standardkonto verwaltet.'
    Write-Host 'Naechster Schritt: Neustart mit  shutdown /r /t 0'
    Write-Host 'Wartung: Strg+Alt+Entf, abmelden und am Administratorkonto anmelden.'
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
