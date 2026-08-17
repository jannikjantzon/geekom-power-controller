#Requires -RunAsAdministrator

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$PlaintextPassword
)

$password = ConvertTo-SecureString $PlaintextPassword -AsPlainText -Force

New-LocalUser `
    -Name "kiosk-power" `
    -Password $password `
    -AccountNeverExpires `
    -PasswordNeverExpires `
    -UserMayNotChangePassword
