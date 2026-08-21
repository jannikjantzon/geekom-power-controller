# Power Controller HTTP API

Alle API-Routen verwenden `POST` und benoetigen den konfigurierten Bearer-Token.

## Einzelnes Geraet

```text
/api/v1/status?id=screen3
/api/v1/startup?id=screen3
/api/v1/shutdown?id=screen3
```

## Alle aktivierten Geraete

```text
/api/v1/startup-all
/api/v1/shutdown-all
```

Die beiden `-all`-Routen akzeptieren keine Query-Parameter. Sie verarbeiten
alle in `config.json` mit `enabled=true` konfigurierten Geraete. Der vorhandene
Controller fuehrt die Aktionen bis zur Grenze `defaults.maxConcurrency`
parallel aus.

Waehrend einer `-all`-Aktion sind Einzelaktionen fuer alle betroffenen Geraete
gesperrt und antworten mit HTTP `409`. Wenn mindestens ein Geraet fehlschlaegt,
enthaelt die Antwort die Einzelergebnisse und verwendet HTTP `502`.

## PowerShell-Beispiel

```powershell
$Headers = @{ Authorization = "Bearer <TOKEN>" }

Invoke-RestMethod `
    -Method Post `
    -Uri "http://192.168.110.12:8787/api/v1/shutdown-all" `
    -Headers $Headers
```

Nach dem Austausch von `api/power_api.py` die geplante Aufgabe neu laden:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\api\startup.ps1
```
