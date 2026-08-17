#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$user = Get-LocalUser -Name "kiosk-power"
$sid = "*$($user.SID.Value)"
$right = "SeRemoteShutdownPrivilege"
$workDirectory = Join-Path $env:TEMP "kiosk-power-rights"
$policyFile = Join-Path $workDirectory "policy.inf"
$databaseFile = Join-Path $workDirectory "policy.sdb"

New-Item -ItemType Directory -Path $workDirectory -Force | Out-Null

try {
    secedit.exe /export /cfg $policyFile /areas USER_RIGHTS /quiet | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Export der lokalen Sicherheitsrichtlinie fehlgeschlagen."
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    Get-Content $policyFile | ForEach-Object { $lines.Add($_) }
    $rightIndex = -1
    $sectionIndex = -1

    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index].Trim() -eq "[Privilege Rights]") {
            $sectionIndex = $index
        }
        if ($lines[$index] -match "^\s*$right\s*=") {
            $rightIndex = $index
            break
        }
    }

    if ($rightIndex -ge 0) {
        $entries = @(($lines[$rightIndex] -split "=", 2)[1] -split "," |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ })

        if ($entries -notcontains $sid) {
            $lines[$rightIndex] = "$right = $(($entries + $sid) -join ',')"
        }
    } else {
        if ($sectionIndex -lt 0) {
            throw "Abschnitt [Privilege Rights] wurde nicht gefunden."
        }
        $lines.Insert($sectionIndex + 1, "$right = $sid")
    }

    Set-Content -Path $policyFile -Value $lines -Encoding Unicode
    secedit.exe /configure /db $databaseFile /cfg $policyFile /areas USER_RIGHTS /quiet | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Zuweisung des Remote-Shutdown-Rechts fehlgeschlagen."
    }

    Write-Host "OK: kiosk-power besitzt jetzt das Remote-Shutdown-Recht."
} finally {
    Remove-Item -Path $workDirectory -Recurse -Force -ErrorAction SilentlyContinue
}
