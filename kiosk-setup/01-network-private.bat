@echo off
fltmc >nul 2>&1 || (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -Command ^
    "$ErrorActionPreference='Stop'; Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private"

if errorlevel 1 (
    echo FEHLER: Netzwerkprofil konnte nicht geaendert werden.
    pause
    exit /b 1
)

echo OK: Ethernet ist jetzt ein privates Netzwerk.
pause