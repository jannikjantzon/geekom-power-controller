@echo off

if "%~1"=="" (
    echo Verwendung: %~nx0 SERVER-IP
    echo Beispiel:   %~nx0 192.168.2.118
    exit /b 2
)

fltmc >nul 2>&1 || (
    echo FEHLER: CMD zuerst als Administrator starten und dieses Skript dort aufrufen.
    exit /b 5
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp003-firewall-icmp.ps1" -ServerIp "%~1"
set "SCRIPT_EXIT_CODE=%ERRORLEVEL%"

if not "%SCRIPT_EXIT_CODE%"=="0" (
    echo FEHLER: ICMP-Firewallregel konnte nicht eingerichtet werden.
)

exit /b %SCRIPT_EXIT_CODE%
