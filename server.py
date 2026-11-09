# ==========================================================
# MyChat Server
# Aufgaben: Accounts, Kontakte, Dateien prüfen, P2P vermitteln
# und Offline-Nachrichten zwischenspeichern ("Briefkasten").
# ==========================================================

import hashlib  # zum Berechnen des "Fingerabdrucks" (SHA-256) einer Datei
import io  # damit wir Bilder im Arbeitsspeicher bearbeiten können (ohne Datei)
import json  # um Nachrichten als Text in der Datenbank zu speichern
import os  # für Dateipfade, Ordner und Umgebungsvariablen
import re  # für "Reguläre Ausdrücke" (Muster zum Prüfen von Text)
import sqlite3  # die eingebaute Datenbank von Python
import time  # für die aktuelle Uhrzeit und zum Warten
import uuid  # erzeugt zufällige, eindeutige IDs (für Dateinamen)
import warnings  # damit wir Warnungen in Fehler umwandeln können
from datetime import timedelta  # für Zeitspannen (z. B. "30 Tage")

import requests  # um Anfragen an VirusTotal ins Internet zu schicken
from PIL import Image  # Pillow: öffnet, prüft und speichert Bilder
from flask import Flask, jsonify, request, send_file, session  # das Web-Framework
from flask_socketio import SocketIO, disconnect, emit  # Echtzeit-Verbindungen
from werkzeug.middleware.proxy_fix import ProxyFix  # liest Infos von nginx richtig ein
from werkzeug.security import check_password_hash, generate_password_hash  # Passwörter sicher speichern
from werkzeug.utils import secure_filename  # macht Dateinamen ungefährlich

# ---------- Einstellungen ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # Ordner, in dem diese Datei liegt
DB_PATH = os.path.join(BASE_DIR, "chat.db")  # Pfad zur Datenbank-Datei
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")  # Ordner für hochgeladene Dateien
os.makedirs(UPLOAD_DIR, exist_ok=True)  # Ordner anlegen, falls er noch fehlt

VT_API_KEY = os.environ.get("VT_API_KEY", "")  # VirusTotal-Schlüssel aus /etc/mychat.env
VT_URL = "https://www.virustotal.com/api/v3"  # Adresse der VirusTotal-Schnittstelle

MAX_PENDING = 500  # höchstens 500 wartende Nachrichten pro Empfänger (Schutz vor Spam)
MAX_TEXT = 2000  # höchstens 2000 Zeichen pro Nachricht

app = Flask(__name__, static_folder="public", static_url_path="")  # App erstellen, Webseite liegt in "public"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # echte IP und http/https von nginx übernehmen
app.config.update(  # mehrere Einstellungen auf einmal setzen
    SECRET_KEY=os.environ.get("SECRET_KEY") or os.urandom(32),  # geheimer Schlüssel für Login-Cookies
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # Uploads größer als 10 MB werden abgelehnt
    SESSION_COOKIE_HTTPONLY=True,  # JavaScript darf das Login-Cookie nicht lesen
    SESSION_COOKIE_SAMESITE="Lax",  # fremde Webseiten können das Cookie nicht mitschicken
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",  # Cookie nur über HTTPS (wenn eingeschaltet)
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # Login bleibt 30 Tage gültig
)  # Ende der Einstellungen
socketio = SocketIO(app)  # Echtzeit-Funktionen an die App anhängen

Image.MAX_IMAGE_PIXELS = 40_000_000  # Bilder mit mehr als 40 Mio. Pixeln sind verdächtig ("Bildbombe")
warnings.simplefilter("error", Image.DecompressionBombWarning)  # Warnung zu Bildbomben wird ein Fehler


# ---------- Datenbank ----------
def query(sql, args=(), one=False):  # Hilfsfunktion: führt einen SQL-Befehl aus
    conn = sqlite3.connect(DB_PATH, timeout=10)  # Verbindung zur Datenbank öffnen
    conn.row_factory = sqlite3.Row  # Ergebnisse wie Wörterbücher lesbar machen (row["name"])
    try:  # versuchen ...
        rows = conn.execute(sql, args).fetchall()  # Befehl ausführen und alle Ergebnisse holen
        conn.commit()  # Änderungen endgültig speichern
    finally:  # ... und auf jeden Fall danach:
        conn.close()  # Verbindung wieder schließen
    if one:  # wenn nur ein Ergebnis gewünscht ist ...
        return rows[0] if rows else None  # ... das erste zurückgeben (oder None, wenn leer)
    return rows  # sonst alle Ergebnisse zurückgeben


# Tabelle für Benutzer: Name, verschlüsseltes Passwort, Erstellungszeit
query("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created INTEGER NOT NULL)")
# Tabelle für Kontakte: wer (owner) hat wen (contact) in seiner Liste
query("CREATE TABLE IF NOT EXISTS contacts (owner TEXT NOT NULL, contact TEXT NOT NULL, PRIMARY KEY (owner, contact))")
# Tabelle für Dateien: zufällige ID, Absender, Empfänger, Name, Dateityp, Zeit
query("CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, owner TEXT NOT NULL, recipient TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, created INTEGER NOT NULL)")
# NEU: Briefkasten für Offline-Nachrichten (werden nach Zustellung gelöscht)
query("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipient TEXT NOT NULL, data TEXT NOT NULL, created INTEGER NOT NULL)")


def get_contacts(user):  # gibt die Kontaktliste eines Benutzers zurück
    rows = query("SELECT contact FROM contacts WHERE owner = ? ORDER BY contact", (user,))  # alle Kontakte, sortiert
    return [r["contact"] for r in rows]  # nur die Namen als Liste zurückgeben


def is_contact(user, other):  # prüft, ob "other" ein Kontakt von "user" ist
    return query("SELECT 1 FROM contacts WHERE owner = ? AND contact = ?", (user, other), one=True) is not None  # True/False


def current_user():  # wer ist gerade eingeloggt?
    return session.get("user")  # Benutzername aus dem Login-Cookie (oder None)


# ---------- Sicherheits-Header (gelten für jede Antwort) ----------
@app.after_request  # diese Funktion läuft nach jeder Anfrage
def security_headers(resp):  # resp = die Antwort, die gleich an den Browser geht
    resp.headers["X-Content-Type-Options"] = "nosniff"  # Browser darf Dateitypen nicht "erraten"
    resp.headers["X-Frame-Options"] = "DENY"  # Seite darf nicht in fremde Seiten eingebettet werden
    resp.headers["Referrer-Policy"] = "no-referrer"  # keine Adressen an andere Seiten verraten
    resp.headers.setdefault(  # nur setzen, wenn noch keine Regel da ist
        "Content-Security-Policy",  # erlaubt nur Skripte/Bilder von uns selbst (+ Socket.IO)
        "default-src 'self'; script-src 'self' https://cdn.socket.io; "  # Skripte nur von uns und Socket.IO
        "connect-src 'self' ws: wss:; img-src 'self'; style-src 'self'; "  # Verbindungen, Bilder, Styles nur von uns
        "frame-ancestors 'none'",  # niemand darf uns einbetten
    )  # Ende der Regel
    return resp  # Antwort mit den neuen Headern zurückgeben


# ---------- Login / Registrieren ----------
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")  # erlaubte Benutzernamen: 3-20 Zeichen a-z, 0-9, _
attempts = {}  # merkt sich Fehlversuche pro IP-Adresse (gegen Passwort-Raten)


def too_many_attempts(ip):  # hat diese IP in 5 Minuten zu oft versucht?
    now = time.time()  # aktuelle Zeit in Sekunden
    attempts[ip] = [t for t in attempts.get(ip, []) if now - t < 300]  # nur Versuche der letzten 300 s behalten
    return len(attempts[ip]) >= 10  # True, wenn es 10 oder mehr waren


def log_in(username):  # meldet einen Benutzer an
    session.clear()  # altes Login-Cookie leeren
    session.permanent = True  # Login soll 30 Tage halten
    session["user"] = username  # Benutzername im Cookie speichern (verschlüsselt signiert)


@app.route("/")  # Startseite
def index():  # wird bei http://.../ aufgerufen
    return app.send_static_file("index.html")  # die Chat-Webseite ausliefern


@app.get("/api/me")  # fragt: bin ich eingeloggt?
def api_me():  # wird beim Laden der Seite aufgerufen
    return jsonify(username=current_user())  # Benutzername (oder null) zurückgeben


@app.post("/api/register")  # neuen Account anlegen
def api_register():  # wird beim Klick auf "Create account" aufgerufen
    ip = request.remote_addr or "?"  # IP-Adresse des Besuchers
    if too_many_attempts(ip):  # zu viele Versuche?
        return jsonify(error="Too many attempts. Try again in 5 minutes."), 429  # ablehnen
    attempts[ip].append(time.time())  # diesen Versuch zählen (gegen Massen-Registrierung)

    data = request.get_json(silent=True) or {}  # gesendete Daten lesen (oder leer)
    username = str(data.get("username", "")).strip().lower()  # Name: Leerzeichen weg, klein geschrieben
    password = str(data.get("password", ""))  # Passwort als Text
    if not USERNAME_RE.match(username):  # passt der Name nicht zum Muster?
        return jsonify(error="Username: 3-20 characters, only a-z, 0-9 and _"), 400  # Fehlermeldung
    if len(password) < 8:  # Passwort zu kurz?
        return jsonify(error="Password must be at least 8 characters"), 400  # Fehlermeldung
    if query("SELECT 1 FROM users WHERE username = ?", (username,), one=True):  # Name schon vergeben?
        return jsonify(error="Username is already taken"), 400  # Fehlermeldung

    query("INSERT INTO users VALUES (?, ?, ?)", (username, generate_password_hash(password), int(time.time())))  # speichern (Passwort nur als Hash)
    log_in(username)  # direkt einloggen
    return jsonify(username=username)  # Erfolg melden


@app.post("/api/login")  # einloggen
def api_login():  # wird beim Klick auf "Log in" aufgerufen
    ip = request.remote_addr or "?"  # IP-Adresse des Besuchers
    if too_many_attempts(ip):  # zu viele Fehlversuche?
        return jsonify(error="Too many attempts. Try again in 5 minutes."), 429  # ablehnen

    data = request.get_json(silent=True) or {}  # gesendete Daten lesen
    username = str(data.get("username", "")).strip().lower()  # Name säubern
    password = str(data.get("password", ""))  # Passwort
    row = query("SELECT password_hash FROM users WHERE username = ?", (username,), one=True)  # gespeicherten Hash holen
    if not row or not check_password_hash(row["password_hash"], password):  # Benutzer fehlt oder Passwort falsch?
        attempts[ip].append(time.time())  # Fehlversuch zählen
        return jsonify(error="Wrong username or password"), 401  # absichtlich ungenau (verrät nicht, was falsch war)

    log_in(username)  # einloggen
    return jsonify(username=username)  # Erfolg melden


@app.post("/api/logout")  # ausloggen
def api_logout():  # wird beim Klick auf ⎋ aufgerufen
    session.clear()  # Login-Cookie leeren
    return jsonify(ok=True)  # Erfolg melden


# ---------- Datei-Prüfung (gegen Viren und PHP-Shells) ----------
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}  # erlaubte Bild-Endungen
ZIP = [b"PK\x03\x04"]  # so beginnen ZIP-Dateien (auch .docx, .xlsx, .pptx)
OTHER_TYPES = {  # erlaubte andere Dateien: Endung -> (Typ, wie der Inhalt beginnen muss)
    ".pdf": ("application/pdf", [b"%PDF-"]),  # PDF beginnt immer mit %PDF-
    ".txt": ("text/plain; charset=utf-8", None),  # Textdatei (wird extra geprüft)
    ".zip": ("application/zip", ZIP),  # ZIP-Archiv
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ZIP),  # Word
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ZIP),  # Excel
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ZIP),  # PowerPoint
    ".mp3": ("audio/mpeg", [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"]),  # MP3-Musik
}  # Ende der Liste


def clean_image(data, ext):  # prüft ein Bild und speichert es sauber neu
    try:  # versuchen ...
        img = Image.open(io.BytesIO(data))  # Bild aus den Bytes öffnen
        img.verify()  # prüfen, ob es wirklich ein gültiges Bild ist
        img = Image.open(io.BytesIO(data))  # nach verify() muss man neu öffnen
        img.load()  # alle Pixel laden
    except Exception:  # ... wenn irgendetwas schiefgeht:
        return None  # kein echtes Bild -> ablehnen

    out = io.BytesIO()  # leerer Speicher für das neue Bild
    if ext in (".jpg", ".jpeg"):  # war es ein JPG?
        img.convert("RGB").save(out, "JPEG", quality=90)  # als neues JPG speichern (versteckter Code fliegt raus)
        return out.getvalue(), "image/jpeg", ".jpg"  # neue Bytes, Typ und Endung zurückgeben
    if img.mode not in ("RGB", "RGBA"):  # ungewöhnliches Farbformat?
        img = img.convert("RGBA")  # in normales Format umwandeln
    img.save(out, "PNG")  # alles andere als neues PNG speichern
    return out.getvalue(), "image/png", ".png"  # neue Bytes, Typ und Endung zurückgeben


def vt_get(url):  # Anfrage an VirusTotal, wartet bei zu vielen Anfragen
    for _ in range(4):  # höchstens 4 Versuche
        r = requests.get(url, headers={"x-apikey": VT_API_KEY}, timeout=20)  # Anfrage mit Schlüssel senden
        if r.status_code != 429:  # 429 = "zu viele Anfragen"; alles andere ...
            return r  # ... sofort zurückgeben
        time.sleep(20)  # 20 Sekunden warten und nochmal versuchen
    return r  # letzte Antwort zurückgeben


def is_clean(stats):  # sagt, ob VirusTotal etwas gefunden hat
    return stats.get("malicious", 0) == 0 and stats.get("suspicious", 0) == 0  # sauber, wenn 0 gefährlich und 0 verdächtig


def virustotal_check(data, filename, image):  # prüft eine Datei mit VirusTotal
    if not VT_API_KEY:  # kein Schlüssel eingetragen?
        if image:  # Bilder sind durch clean_image schon gereinigt ...
            return True, ""  # ... also erlaubt
        return False, "Virus scanner is not set up (VT_API_KEY missing)"  # andere Dateien ablehnen

    try:  # versuchen (Internet kann ausfallen)
        sha256 = hashlib.sha256(data).hexdigest()  # Fingerabdruck berechnen
        r = vt_get(f"{VT_URL}/files/{sha256}")  # 1. nur Fingerabdruck nachschlagen (privat!)
        if r.status_code == 200:  # VirusTotal kennt die Datei schon
            stats = r.json()["data"]["attributes"]["last_analysis_stats"]  # Ergebnis der Scanner
            if is_clean(stats):  # nichts gefunden?
                return True, ""  # erlaubt
            return False, f"Blocked: {stats.get('malicious', 0)} virus scanners found a threat"  # gefährlich
        if r.status_code != 404:  # 404 = unbekannt; alles andere ist ein Fehler
            return False, "Virus check failed, please try again"  # sicherheitshalber ablehnen

        if image:  # unbekanntes Bild: wurde schon gereinigt ...
            return True, ""  # ... also erlaubt (private Fotos werden nicht hochgeladen)
        r = requests.post(f"{VT_URL}/files", headers={"x-apikey": VT_API_KEY}, files={"file": (filename, data)}, timeout=60)  # 2. Datei hochladen
        if r.status_code != 200:  # Upload fehlgeschlagen?
            return False, "Virus check failed, please try again"  # ablehnen
        analysis_id = r.json()["data"]["id"]  # Nummer der Untersuchung merken

        for _ in range(9):  # 3. bis zu 9 mal nachfragen (= 3 Minuten)
            time.sleep(20)  # 20 Sekunden warten
            r = vt_get(f"{VT_URL}/analyses/{analysis_id}")  # nach dem Ergebnis fragen
            if r.status_code != 200:  # keine gültige Antwort?
                continue  # nächster Versuch
            attrs = r.json()["data"]["attributes"]  # Ergebnis lesen
            if attrs.get("status") == "completed":  # fertig untersucht?
                if is_clean(attrs.get("stats", {})):  # sauber?
                    return True, ""  # erlaubt
                return False, "Blocked: virus scanners found a threat"  # gefährlich
        return False, "Virus check took too long, please try again"  # nach 3 Minuten aufgeben
    except (requests.RequestException, KeyError, ValueError):  # Internet- oder Antwortfehler
        return False, "Virus check failed, please try again"  # sicherheitshalber ablehnen


@app.post("/upload")  # Datei hochladen
def upload():  # wird beim Klick auf 📎 aufgerufen
    user = current_user()  # wer lädt hoch?
    if not user:  # nicht eingeloggt?
        return jsonify(error="Not logged in"), 401  # ablehnen
    to = str(request.form.get("to", "")).lower()  # an wen geht die Datei?
    if not is_contact(user, to):  # nur an eigene Kontakte erlaubt
        return jsonify(error="Not your contact"), 403  # ablehnen

    file = request.files.get("file")  # die hochgeladene Datei
    if not file or not file.filename:  # keine Datei dabei?
        return jsonify(error="No file"), 400  # ablehnen
    name = secure_filename(file.filename) or "file"  # Dateinamen ungefährlich machen
    ext = os.path.splitext(name)[1].lower()  # Endung, z. B. ".png"
    data = file.read()  # gesamten Inhalt lesen

    if ext in IMAGE_TYPES:  # ist es ein Bild?
        cleaned = clean_image(data, ext)  # prüfen und neu speichern
        if not cleaned:  # kein echtes Bild?
            return jsonify(error="This is not a valid image"), 400  # ablehnen (z. B. getarnte PHP-Shell)
        saved_data, mime, new_ext = cleaned  # gereinigte Bytes, Typ, Endung
        name = os.path.splitext(name)[0] + new_ext  # Namen an neue Endung anpassen
        image = True  # merken: ist ein Bild
    elif ext in OTHER_TYPES:  # ist es ein anderer erlaubter Typ?
        mime, starts = OTHER_TYPES[ext]  # Typ und erwarteter Anfang
        if starts and not any(data.startswith(s) for s in starts):  # Inhalt passt nicht zur Endung?
            return jsonify(error="File content does not match its type"), 400  # ablehnen
        if ext == ".txt":  # Textdatei?
            try:  # versuchen ...
                data.decode("utf-8")  # ... als Text zu lesen
            except UnicodeDecodeError:  # geht nicht -> kein Text
                return jsonify(error="Not a text file"), 400  # ablehnen
        if b"<?php" in data.lower():  # enthält die Datei PHP-Code?
            return jsonify(error="Blocked: file contains PHP code"), 400  # ablehnen
        saved_data = data  # unverändert speichern
        image = False  # merken: kein Bild
    else:  # alle anderen Endungen (.exe, .php, .html, ...)
        return jsonify(error="This file type is not allowed"), 400  # ablehnen

    ok, reason = virustotal_check(data, name, image)  # mit VirusTotal prüfen
    if not ok:  # gefährlich oder Prüfung fehlgeschlagen?
        return jsonify(error=reason), 400  # ablehnen mit Grund

    file_id = uuid.uuid4().hex  # zufälliger Name, z. B. "3f9a..."
    with open(os.path.join(UPLOAD_DIR, file_id), "wb") as f:  # Datei ohne Endung anlegen -> nie ausführbar
        f.write(saved_data)  # Inhalt hineinschreiben
    query("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)", (file_id, user, to, name, mime, int(time.time())))  # in Datenbank eintragen
    return jsonify(id=file_id, name=name, image=image)  # ID an den Browser zurückgeben


@app.get("/files/<file_id>")  # Datei abrufen
def get_file(file_id):  # wird beim Anzeigen/Herunterladen aufgerufen
    user = current_user()  # wer fragt?
    if not user or not re.fullmatch(r"[a-f0-9]{32}", file_id):  # nicht eingeloggt oder ungültige ID?
        return "Not found", 404  # so tun, als gäbe es sie nicht
    row = query("SELECT * FROM files WHERE id = ?", (file_id,), one=True)  # Datei in der Datenbank suchen
    if not row or user not in (row["owner"], row["recipient"]):  # nur Absender und Empfänger dürfen sie sehen
        return "Not found", 404  # alle anderen bekommen "nicht gefunden"

    resp = send_file(os.path.join(UPLOAD_DIR, file_id), mimetype=row["mime"],  # Datei mit richtigem Typ senden
                     as_attachment=not row["mime"].startswith("image/"),  # Nicht-Bilder werden heruntergeladen
                     download_name=row["name"])  # ursprünglicher Dateiname beim Download
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"  # Datei darf im Browser nichts ausführen
    return resp  # Antwort senden


# ---------- Echtzeit (Socket.IO) ----------
online = {}  # wer ist online: Benutzername -> Verbindungs-ID
sid_user = {}  # umgekehrt: Verbindungs-ID -> Benutzername


def tell_contacts(user, is_online):  # sagt allen Kontakten, ob "user" online/offline ist
    for contact in get_contacts(user):  # für jeden Kontakt ...
        if contact in online:  # ... der gerade online ist ...
            socketio.emit("presence", {"name": user, "online": is_online}, to=online[contact])  # ... Bescheid geben


@socketio.on("connect")  # ein Browser verbindet sich
def on_connect(*args):  # *args = eventuelle Zusatzdaten (brauchen wir nicht)
    user = current_user()  # wer ist das? (aus dem Login-Cookie)
    if not user:  # nicht eingeloggt?
        return False  # Verbindung ablehnen

    old_sid = online.get(user)  # war derselbe Benutzer schon in einem anderen Fenster online?
    online[user] = request.sid  # neue Verbindung merken
    sid_user[request.sid] = user  # auch umgekehrt merken
    if old_sid:  # gab es eine alte Verbindung?
        emit("kicked", to=old_sid)  # altem Fenster sagen: du bist jetzt inaktiv
        disconnect(sid=old_sid)  # alte Verbindung trennen

    emit("contacts", [{"name": c, "online": c in online} for c in get_contacts(user)])  # Kontaktliste mit Online-Status schicken
    tell_contacts(user, True)  # Kontakten sagen: ich bin jetzt online


@socketio.on("add_contact")  # Kontakt hinzufügen
def on_add_contact(name):  # name = eingegebener Benutzername
    user = sid_user.get(request.sid)  # wer fragt?
    name = str(name).strip().lower()  # Namen säubern
    if not user:  # nicht eingeloggt?
        return {"ok": False, "error": "Not logged in"}  # Fehler zurück
    if name == user:  # sich selbst hinzufügen?
        return {"ok": False, "error": "You can't add yourself"}  # Fehler zurück
    if not query("SELECT 1 FROM users WHERE username = ?", (name,), one=True):  # gibt es den Benutzer?
        return {"ok": False, "error": "User not found"}  # Fehler zurück
    if is_contact(user, name):  # schon in der Liste?
        return {"ok": False, "error": "Already in your contacts"}  # Fehler zurück

    query("INSERT OR IGNORE INTO contacts VALUES (?, ?)", (user, name))  # ich habe ihn als Kontakt
    query("INSERT OR IGNORE INTO contacts VALUES (?, ?)", (name, user))  # er hat mich als Kontakt (gegenseitig)
    emit("contact-added", {"name": name, "online": name in online})  # mir den neuen Kontakt anzeigen
    if name in online:  # ist der andere gerade online?
        emit("contact-added", {"name": user, "online": True}, to=online[name])  # ihm mich anzeigen
    return {"ok": True}  # Erfolg


@socketio.on("signal")  # P2P-Verbindungsdaten weiterleiten ("Signaling")
def on_signal(data):  # data = {to: Empfänger, signal: Verbindungsdaten}
    user = sid_user.get(request.sid)  # wer schickt?
    if not user or not isinstance(data, dict):  # nicht eingeloggt oder kaputte Daten?
        return  # ignorieren
    to = str(data.get("to", ""))  # an wen?
    if to in online and is_contact(user, to):  # nur an Kontakte, die online sind
        emit("signal", {"from": user, "signal": data.get("signal")}, to=online[to])  # weiterleiten


# ---------- NEU: Offline-Nachrichten ("Briefkasten") ----------
def clean_message(data, sender, recipient):  # prüft eine Nachricht, bevor der Server sie annimmt
    if not isinstance(data, dict):  # keine richtige Nachricht?
        return None  # ablehnen
    if data.get("type") == "text":  # Textnachricht?
        text = str(data.get("text", "")).strip()[:MAX_TEXT]  # Text säubern und kürzen
        return {"type": "text", "text": text} if text else None  # leere Texte ablehnen
    if data.get("type") == "file":  # Datei-Nachricht?
        file_id = str(data.get("id", ""))  # ID der Datei
        if not re.fullmatch(r"[a-f0-9]{32}", file_id):  # ID hat falsches Format?
            return None  # ablehnen
        row = query("SELECT name, mime FROM files WHERE id = ? AND owner = ? AND recipient = ?",  # Datei muss vom Absender ...
                    (file_id, sender, recipient), one=True)  # ... an genau diesen Empfänger hochgeladen worden sein
        if not row:  # nicht gefunden?
            return None  # ablehnen
        return {"type": "file", "id": file_id, "name": row["name"], "image": row["mime"].startswith("image/")}  # Angaben aus der Datenbank (nicht vom Browser!)
    return None  # unbekannter Typ -> ablehnen


@socketio.on("send_message")  # Nachricht über den Server schicken (wenn P2P nicht geht)
def on_send_message(data):  # data = {to: Empfänger, message: Nachricht}
    user = sid_user.get(request.sid)  # wer schickt?
    if not user or not isinstance(data, dict):  # nicht eingeloggt oder kaputte Daten?
        return {"ok": False, "error": "Not logged in"}  # Fehler zurück
    to = str(data.get("to", "")).lower()  # an wen?
    if not is_contact(user, to):  # nur an eigene Kontakte
        return {"ok": False, "error": "Not your contact"}  # Fehler zurück
    msg = clean_message(data.get("message"), user, to)  # Nachricht prüfen
    if not msg:  # ungültig?
        return {"ok": False, "error": "Invalid message"}  # Fehler zurück
    msg["time"] = int(time.time() * 1000)  # Zeitstempel vom Server (Millisekunden)

    if to in online:  # Empfänger ist online (nur P2P klappt gerade nicht)?
        emit("message", dict(msg, **{"from": user}), to=online[to])  # direkt über den Server zustellen
        return {"ok": True, "delivered": True}  # Erfolg: zugestellt (✓✓)

    count = query("SELECT COUNT(*) AS n FROM pending WHERE recipient = ?", (to,), one=True)["n"]  # wie voll ist sein Briefkasten?
    if count >= MAX_PENDING:  # zu voll?
        return {"ok": False, "error": "Mailbox of " + to + " is full"}  # Fehler zurück
    query("INSERT INTO pending (sender, recipient, data, created) VALUES (?, ?, ?, ?)",  # in den Briefkasten legen
          (user, to, json.dumps(msg), msg["time"]))  # Nachricht als JSON-Text speichern
    return {"ok": True, "delivered": False}  # Erfolg: liegt im Briefkasten (✓)


@socketio.on("fetch_pending")  # Browser fragt: liegen Nachrichten für mich bereit?
def on_fetch_pending():  # wird direkt nach dem Verbinden aufgerufen
    user = sid_user.get(request.sid)  # wer fragt?
    if not user:  # nicht eingeloggt?
        return []  # leere Liste
    rows = query("SELECT id, sender, data FROM pending WHERE recipient = ? ORDER BY id", (user,))  # alle Nachrichten, älteste zuerst
    if rows:  # gibt es welche?
        query("DELETE FROM pending WHERE recipient = ? AND id <= ?", (user, rows[-1]["id"]))  # sofort aus dem Briefkasten löschen
    return [dict(json.loads(r["data"]), **{"from": r["sender"]}) for r in rows]  # Nachrichten mit Absender zurückgeben


@socketio.on("disconnect")  # ein Browser trennt sich
def on_disconnect(*args):  # *args = Grund (brauchen wir nicht)
    user = sid_user.pop(request.sid, None)  # Benutzer zu dieser Verbindung holen und entfernen
    if user and online.get(user) == request.sid:  # war das seine aktuelle Verbindung?
        del online[user]  # als offline markieren
        tell_contacts(user, False)  # Kontakten sagen: ich bin offline


# ---------- Start (nur beim Testen auf dem eigenen PC) ----------
if __name__ == "__main__":  # nur wenn man "python server.py" startet (nicht über gunicorn)
    print("Chat running on http://localhost:8000")  # Hinweis im Terminal
    socketio.run(app, host="127.0.0.1", port=8000, allow_unsafe_werkzeug=True)  # Testserver starten
