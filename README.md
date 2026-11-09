# MyChat

Ein selbst gehosteter Chat im Stil von WhatsApp - mit Python (Flask + Socket.IO) und WebRTC.

## Funktionen

- **Accounts** mit Benutzername und Passwort (Passwörter nur als Hash gespeichert)
- **Kontakte** hinzufügen über den Benutzernamen
- **Private Chats per P2P**: Nachrichten gehen direkt von Browser zu Browser (WebRTC)
- **Offline-Nachrichten**: Ist der Empfänger offline, wartet die Nachricht auf dem Server und wird nach der Zustellung gelöscht (✓ = wartet, ✓✓ = zugestellt)
- **Sprach- und Videoanrufe** per WebRTC (benötigt HTTPS)
- **Bilder und Dateien** senden (max. 10 MB)
- **Virenschutz**: Bilder werden neu gespeichert (entfernt versteckten Code), Dateitypen werden geprüft, optional Prüfung mit VirusTotal
- Chatverlauf wird im Browser gespeichert

## Dateien

| Datei | Inhalt |
|---|---|
| `server.py` | Server: Login, Kontakte, Datei-Prüfung, Offline-Briefkasten, Signaling für P2P |
| `public/index.html` | Aufbau der Webseite |
| `public/style.css` | Aussehen |
| `public/app.js` | Chat-Logik im Browser |
| `public/call.js` | Sprach- und Videoanrufe |
| `install-mychat.sh` | Installiert alles auf einem Ubuntu/Debian-Server (Port 8088) |
| `tailscale-funnel.sh` | Macht den Chat per Tailscale Funnel über HTTPS im Internet erreichbar |

## Lokal starten

```bash
pip install flask flask-socketio simple-websocket requests pillow
python server.py
```

Dann `http://localhost:8000` öffnen.

## Auf einem Server installieren (Ubuntu/Debian)

```bash
sudo bash install-mychat.sh
```

Der Chat läuft danach unter `http://SERVER-IP:8088`.
Für HTTPS (nötig für Anrufe) über Tailscale:

```bash
sudo bash tailscale-funnel.sh
```

## Einstellungen

Werden in `/etc/mychat.env` gespeichert (nicht im Repository):

- `SECRET_KEY` - geheimer Schlüssel für Login-Cookies (wird automatisch erzeugt)
- `VT_API_KEY` - optional, Schlüssel von virustotal.com für die Virenprüfung
