# GEEKOM Power Controller 1.1.1

Backend-neutraler, zentraler Power-Controller fuer kabelgebundene Windows-Kiosk-PCs:

- `wake`: Wake-on-LAN senden und den Start pruefen
- `shutdown`: vom Server aus einen agentlosen Windows-RPC-Shutdown ausloesen und den Offline-Zustand pruefen
- `status`: Erreichbarkeit aller oder ausgewaehlter PCs pruefen
- `validate`: Konfiguration ohne Netzwerkaktion validieren

Auf den Mini-PCs wird kein eigener Shutdown-Client, HTTP-Agent oder OpenSSH installiert. Der Controller verwendet die vorhandenen Windows-RPC-/SMB-Dienste. Der Server ist der einzige aktive Steuerungsdienst.

Ein Magic Packet besitzt keine Empfangsbestaetigung. Darum werden MAC und feste IP bzw. stabiler Hostname konfiguriert. Der Controller prueft anschliessend einen TCP-Port, standardmaessig SMB auf Port 445.

## Voraussetzungen

- Python 3.11 oder neuer auf dem Server
- Mini-PCs per RJ45-Ethernet
- Fuer den hier verwendeten A5 5825U: BIOS 2.43 und EC 1.16
- DHCP-Reservierung oder feste IP je Mini-PC
- Windows 11 Pro auf den Mini-PCs, damit die lokale Sicherheitsrichtlinie komfortabel gesetzt werden kann
- RPC/SMB nur im vertrauenswuerdigen Management-LAN

Servervarianten:

- Windows-Server: eingebautes `shutdown.exe`
- Linux-Server: Samba-Werkzeug `net rpc shutdown`, beispielsweise Paket `samba-common-bin`

Dein vorgesehener Ablauf ist damit direkt abgedeckt:

1. Entwicklung/Test auf einem normalen Windows-PC mit `config.example.json` und `windows-native`.
2. Produktion auf Proxmox in einem Debian-basierten Container mit `config.proxmox-docker.example.json` und `samba-rpc`.

## Installation

### Windows-Server

```powershell
New-Item -ItemType Directory -Force C:\PowerController
Copy-Item .\powerctl.py C:\PowerController\powerctl.py
Copy-Item .\config.example.json C:\PowerController\config.json
```

In `config.json` verwenden:

```json
"shutdown": {
  "transport": "windows-native",
  "executable": "C:\\Windows\\System32\\shutdown.exe",
  "commandTimeoutSeconds": 30
}
```

### Linux-Server

Debian/Ubuntu-Beispiel:

```bash
sudo apt install python3 samba-common-bin
sudo install -d -m 0755 /opt/geekom-power-controller
sudo install -d -m 0750 /etc/geekom-power-controller/secrets
sudo install -m 0755 powerctl.py /opt/geekom-power-controller/powerctl.py
sudo cp config.example.json /etc/geekom-power-controller/config.json
```

In `config.json` verwenden:

```json
"shutdown": {
  "transport": "samba-rpc",
  "executable": "/usr/bin/net",
  "credentialFile": "./secrets/samba-auth.conf",
  "commandTimeoutSeconds": 30
}
```

`secrets/samba-auth.conf`:

```text
username = kiosk-power
password = EIN_LANGES_ZUFALLSPASSWORT
domain = WORKGROUP
```

Dateirechte begrenzen:

```bash
chmod 600 /etc/geekom-power-controller/secrets/samba-auth.conf
```

Passwoerter duerfen nicht als Kommandozeilenargument oder direkt in `config.json` stehen. In einer Active-Directory-Umgebung ist ein dediziertes Dienstkonto mit Kerberos einer gemeinsamen Workgroup-Anmeldung vorzuziehen.

### Proxmox/Docker

Unter `docker/` liegen ein eigenstaendiges Dockerfile, ein Compose-Beispiel und ein Snippet fuer ein bestehendes NestJS-Image.

Manueller Build und Test:

```bash
cd geekom-power-controller
docker build -f docker/Dockerfile -t geekom-powerctl:1.1 .

docker run --rm \
  --network host \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -v "$PWD/config.proxmox-docker.json:/config/config.json:ro" \
  -v "$PWD/secrets/samba-auth.conf:/run/secrets/samba-auth.conf:ro" \
  geekom-powerctl:1.1 \
  --config /config/config.json --pretty validate
```

Fuer WoL ist `--network host` unter Linux die robuste Variante, weil der Container den Layer-2-Broadcast des Kiosk-Netzes erreichen muss. In einer VM oder einem LXC unter Proxmox muss die virtuelle Netzwerkkarte an der richtigen Bridge beziehungsweise am richtigen VLAN haengen.

Wenn NestJS den Controller direkt per `spawn()` ausfuehrt, Python, `net` und `powerctl.py` in dasselbe Backend-Image aufnehmen; siehe `docker/NestJS.Dockerfile.snippet`. Dem Backend niemals `/var/run/docker.sock` geben und nicht `docker exec` aus der Webanwendung aufrufen. Alternativ kann spaeter eine getrennte interne API um den Controller gebaut werden.

## Geraetekonfiguration

`config.example.json` kopieren und anpassen:

- `broadcast`: Broadcast-Adresse des Kiosk-Netzes
- `id`: interne stabile ID
- `mac`: MAC des kabelgebundenen Ethernet-Adapters
- `host`: reservierte IPv4-Adresse oder DNS-Name
- `healthCheck.port`: stabil erreichbarer TCP-Port, standardmaessig SMB `445`

Beispiel:

```json
{
  "id": "kiosk-01",
  "mac": "AA:BB:CC:DD:EE:01",
  "host": "192.168.2.21"
}
```

Windows-Werte kontrollieren:

```powershell
Get-NetAdapter -Physical | Format-Table Name, MacAddress, Status, LinkSpeed
Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress, PrefixLength
```

## CLI

Konfiguration pruefen:

```bash
python3 powerctl.py --config config.json --pretty validate
```

Ohne Device-IDs werden alle Eintraege mit `enabled: true` verarbeitet:

```bash
python3 powerctl.py --config config.json --pretty status
python3 powerctl.py --config config.json --pretty wake
python3 powerctl.py --config config.json --pretty shutdown
```

Auswahl:

```bash
python3 powerctl.py --config config.json --pretty wake kiosk-01 kiosk-02
```

Exit-Codes:

| Code | Bedeutung |
|---:|---|
| `0` | Alle Operationen erfolgreich |
| `10` | Mindestens ein Geraet fehlgeschlagen |
| `20` | Konfiguration ungueltig |
| `21` | Device-Auswahl ungueltig |
| `30` | Interner Fehler |

Stdout enthaelt genau ein JSON-Dokument. Fehlgeschlagene Geraete stehen in `failedDeviceIds` und `failedMacs`. `wake` und `shutdown` sind idempotent: Bereits online gilt bei `wake` als Erfolg, bereits offline bei `shutdown` ebenfalls.

## Windows fuer Wake-on-LAN vorbereiten

Im BIOS:

- `Wake on LAN` bzw. `LAN Wake`: `Enabled`, falls sichtbar
- `ErP` bzw. `Deep Sleep`: `Disabled`, falls vorhanden
- `Power Loss Policy` ist unabhaengig von WoL

Beim kabelgebundenen Realtek-Adapter im Geraete-Manager:

- `Wake on Magic Packet`: aktiviert
- `Shutdown Wake-On-Lan` bzw. `Wake From Shutdown`: aktiviert
- `Geraet kann den Computer aus dem Ruhezustand aktivieren`: aktiviert
- `Nur Magic Packet kann den Computer aktivieren`: aktiviert

Fast Startup fuer einen eindeutigen S5-Shutdown deaktivieren:

```powershell
powercfg /hibernate off
```

## Windows fuer agentlosen Remote-Shutdown vorbereiten

### 1. Dediziertes Konto

Auf jedem Kiosk-PC ein lokales Konto `kiosk-power` mit einem langen Zufallspasswort anlegen. Dieses Konto nicht fuer die interaktive Kiosk-Sitzung verwenden.

Bei einer Workgroup muss der Linux-Samba-Transport dieses Konto verwenden. Bei einem Windows-Server ohne Active Directory muss der Backend-Dienst in einem passenden Sicherheitskontext laufen; identische lokale Benutzernamen und Passwoerter sind moeglich, aber eine Domain ist langfristig sauberer.

### 2. Remote-Shutdown-Recht

Auf jedem Kiosk-PC `secpol.msc` oeffnen:

```text
Lokale Richtlinien
  -> Zuweisen von Benutzerrechten
  -> Herunterfahren des Systems von einem Remotesystem aus erzwingen
  -> kiosk-power hinzufuegen
```

Der technische Name ist `SeRemoteShutdownPrivilege`. Dieses Recht unterscheidet sich von `Herunterfahren des Systems`, das nur lokales Herunterfahren betrifft. Die Berechtigung eng auf das Dienstkonto begrenzen.

### 3. Windows-Dienste und Netzwerkprofil

PowerShell als Administrator:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
Set-Service LanmanServer -StartupType Automatic
Start-Service LanmanServer
```

SMB1 nicht aktivieren. Aktuelle Windows-/Samba-Versionen sollen SMB2/3 verwenden.

### 4. Firewall gezielt freigeben

In `wf.msc` nur fuer das private Management-Netz beziehungsweise nur fuer die Server-IP aktivieren:

- Remote Service Management: RPC, Named Pipes und RPC Endpoint Mapper
- File and Printer Sharing: SMB-In

Vorhandene Regeln anzeigen:

```powershell
Get-NetFirewallRule |
    Where-Object {
        $_.Name -like "RemoteSvcAdmin*" -or
        $_.Name -eq "FPS-SMB-In-TCP"
    } |
    Format-Table Name, DisplayName, Enabled, Profile
```

Regeln anschliessend in der erweiterten Firewall-GUI auf die IP des zentralen Servers begrenzen. RPC/SMB niemals ins Internet freigeben.

### 5. Direkter Transporttest

Vom Windows-Server:

```cmd
shutdown.exe /m \\192.168.2.21 /s /t 0 /f
```

Vom Linux-Server:

```bash
net rpc shutdown \
  -I 192.168.2.21 \
  -A /etc/geekom-power-controller/secrets/samba-auth.conf \
  -f -t 0
```

Typische Fehler:

- `Access denied`: falscher Sicherheitskontext oder `SeRemoteShutdownPrivilege` fehlt
- `Network path not found`: Firewall, SMB/RPC, falsche IP oder falsches VLAN
- Port 445 nicht erreichbar: Windows-Netzwerkprofil, Firewall oder `LanmanServer`

## Backend-Integration

Das Backend startet das Programm ohne Shell und liest stdout als JSON. Ein NestJS-Beispiel liegt unter `examples/nestjs-power.service.ts`.

Das Beispiel waehlt fuer Windows und Linux passende Standardpfade. Sie koennen explizit gesetzt werden:

```text
POWERCTL_PYTHON
POWERCTL_SCRIPT
POWERCTL_CONFIG
```

Der API-Endpunkt darf nur vordefinierte Device-IDs akzeptieren und benoetigt:

- Authentifizierung
- rollenbasierte Autorisierung
- Rate-Limit
- Audit-Logging
- keine frei uebergebbaren Hostnamen, MACs oder Befehle

## Netzwerk

- WoL funktioniert direkt typischerweise nur im selben VLAN/Subnetz.
- Fuer andere VLANs ein kontrolliertes WoL-Relay verwenden.
- Directed Broadcast nicht global auf dem Router freigeben.
- RPC/SMB nur zwischen zentralem Server und Kiosk-Netz erlauben.
- DHCP-Reservierungen statt zufaelliger statischer Adressen verwenden.

## Rollout

1. Nur einen Pilot-PC in `config.json` aktivieren.
2. RPC-Shutdown manuell vom Server testen.
3. `validate` und `status` testen.
4. `shutdown kiosk-01` testen und das Offline-Ergebnis kontrollieren.
5. `wake kiosk-01` mindestens zehnmal aus echtem S5 testen.
6. Erst danach die restlichen Mini-PCs eintragen.
7. Backend-Endpunkt zuletzt aktivieren.
