@echo off
fltmc >nul 2>&1 || (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -Command ^
    "$ErrorActionPreference='Stop'; Set-Service LanmanServer -StartupType Automatic; Start-Service LanmanServer"

if errorlevel 1 (
    echo FEHLER: LanmanServer konnte nicht gestartet werden.
    pause
    exit /b 1
)

echo OK: LanmanServer laeuft und startet automatisch.
pause