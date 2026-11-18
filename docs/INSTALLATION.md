# GTA Clipper – Automatik: Installation und Betrieb

Die Automatik ist ein kleiner Server, der **nur auf deinem eigenen PC** läuft. Er sucht nach Zeitplan neue GTA-6-Videos
deiner Creator, nimmt Live-Streams auf, lässt Claude (über die Claude API) die besten Momente auswählen und rendert
fertige Shorts. Gesteuert wird alles über ein Dashboard im Browser. API-Schlüssel, YouTube- und Instagram-Verbindung, Quellen und
Zeitplan trägst du dort ein – nach der Installation musst du keine Dateien mehr bearbeiten.

Diese Anleitung bringt dich von einem Rechner ohne alles bis zum laufenden, eingerichteten Dashboard. Du musst ein
Terminal öffnen und Befehle hineinkopieren können, mehr nicht.

**Inhalt**

1. [Schnellstart](#1-schnellstart)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Installation](#3-installation) – [Linux](#linux-ubuntu-debian) · [macOS](#macos) · [Windows (WSL2)](#windows-mit-wsl2)
4. [Erster Start und Einrichtung](#4-erster-start-und-einrichtung)
5. [Tägliche Bedienung](#5-tägliche-bedienung)
6. [YouTube verbinden](#6-youtube-verbinden-optional)
7. [Instagram verbinden](#7-instagram-verbinden-optional)
8. [Dauerbetrieb (Autostart)](#8-dauerbetrieb-autostart)
9. [Zugriff vom Handy im Heimnetz](#9-zugriff-vom-handy-im-heimnetz)
10. [Aktualisieren](#10-aktualisieren)
11. [Datensicherung](#11-datensicherung)
12. [Zugang verloren](#12-zugang-verloren)
13. [Fehlersuche](#13-fehlersuche)
14. [Deinstallieren](#14-deinstallieren)
15. [Rechtliches](#15-rechtliches)
16. [Alle Befehle auf einen Blick](#16-alle-befehle-auf-einen-blick)

---

## 1. Schnellstart

Für **Ubuntu/Debian** (unter Windows: erst [WSL2 einrichten](#windows-mit-wsl2), dann dasselbe im Ubuntu-Fenster):

```bash
sudo apt update
sudo apt install -y git curl ffmpeg espeak-ng fonts-dejavu-core
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Terminal schließen, ein **neues** öffnen, dann:

```bash
git clone https://github.com/antonplatter-spec/clipping-gta-6.git ~/clipping-gta-6
cd ~/clipping-gta-6
./start.sh
```

Der Browser öffnet den Einrichtungsassistenten (sonst den Link hinter *Einrichtung:* aus dem Terminal selbst öffnen).
Er führt dich durch den Rest – weiter in [Abschnitt 4](#4-erster-start-und-einrichtung).

Auf dem **Mac** sieht es fast genauso aus, nur mit Homebrew statt `apt`: siehe [macOS](#macos).

---

## 2. Voraussetzungen

**Betriebssystem**

| System | Unterstützt | Hinweise |
| --- | --- | --- |
| Ubuntu 22.04/24.04, Debian 12/13 (64 Bit) | ja | Getestet auf Ubuntu 24.04. |
| macOS 14 (Sonoma) oder neuer, **Apple-Chip** (M1 oder neuer) | ja | Intel-Macs gehen nicht: für sie gibt es keine aktuellen Pakete der Spracherkennung (onnxruntime). |
| Windows 10/11 | über **WSL2** (Ubuntu) | Der Clipper läuft im Linux-Teil von Windows; das Dashboard öffnest du im normalen Windows-Browser. |

**Speicherplatz** – der größte Posten sind Videos:

| Was | Größe |
| --- | --- |
| Programm und Python-Pakete | rund 400 MB (Download etwa 120 MB) |
| Spracherkennung Parakeet (lädt die erste Analyse von huggingface.co) | etwa 650 MB |
| Optional: Stimmen-Trennung (Demucs mit PyTorch) | Linux: rund 3 GB Download, gut 5 GB installiert; Mac: rund 250 MB Download |
| Quellvideos und Live-Aufnahmen (1080p) | je nach Menge viele GB; werden automatisch gelöscht (siehe unten) |
| Fertige Shorts in `exports/` | je Video eine MP4 pro Hook-Variante in 1080×1920, grob geschätzt einige zehn MB pro MP4; bleiben, bis du sie löschst |

Alte Videos räumt der Server selbst weg: Live-Aufnahmen 3 Stunden nachdem sie geclippt wurden, andere Quellvideos
nach 2 Tagen ohne Änderung. Transkripte, Picks und fertige Shorts bleiben. Fällt der freie Platz unter **30 GB**,
zeigt das Dashboard einen Alarm, und neue Downloads und Live-Aufnahmen warten, bis wieder Platz ist (einstellbar unter
*Einstellungen → Analyse → Alarm unter freiem Speicher*). Plane deshalb **mindestens 50 GB freien Platz** ein; wenn du
viele Live-Streams aufnimmst, deutlich mehr.

**Rechenleistung:** Transkription und Rendern laufen auf der CPU (Stimmen-Trennung nutzt eine NVIDIA-Grafikkarte
bzw. den Apple-Chip, wenn vorhanden). Wie viele Live-Streams dein Rechner gleichzeitig schafft, misst später
`uv run python -m clipper bench` im Clipper-Ordner.

**Konten und Schlüssel** (alles trägst du später im Dashboard ein):

- **Authenticator-App** auf dem Handy (z. B. Google Authenticator, Aegis, 2FAS, 1Password) – Pflicht, das Dashboard
  ist mit Passwort *und* 2FA geschützt.
- **Anthropic-API-Schlüssel** – Pflicht für die Automatik, kostet nach Verbrauch
  (siehe [Schlüssel](#schritt-4-schlüssel)).
- **Fish-Audio-Schlüssel** – optional, für KI-Voiceover und Dubs.
- **Google-Konto mit YouTube-Kanal** – optional, nur wenn der Clipper direkt zu YouTube hochladen soll.
- **Professionelles Instagram-Konto** (Creator oder Business) und ein Facebook-Konto für die Meta-App – optional, nur
  wenn der Clipper Reels direkt auf Instagram posten soll.

---

## 3. Installation

Du brauchst drei Dinge: ein paar Systemprogramme (git, ffmpeg …), **uv** (installiert Python und die Python-Pakete
des Clippers in den Projektordner, ohne dein System anzufassen) und den Clipper selbst.

### Linux (Ubuntu, Debian)

**1. Systemprogramme** – git zum Herunterladen, curl für den uv-Installer, ffmpeg für Ton und Video, espeak-ng als
Ersatzstimme fürs Voiceover (wenn du keinen Fish-Audio-Schlüssel nutzt), eine Schrift-Grundausstattung:

```bash
sudo apt update
sudo apt install -y git curl ffmpeg espeak-ng fonts-dejavu-core
```

`sudo` fragt nach deinem Linux-Passwort; beim Tippen erscheinen keine Zeichen, das ist normal.

**2. uv installieren** (der offizielle Installer von astral.sh, landet in `~/.local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Danach das Terminal schließen und ein neues öffnen, damit `uv` gefunden wird.

**3. Clipper herunterladen** – in deinen Home-Ordner:

```bash
git clone https://github.com/antonplatter-spec/clipping-gta-6.git ~/clipping-gta-6
```

**4. Starten:**

```bash
cd ~/clipping-gta-6
./start.sh
```

Weiter mit [Abschnitt 4](#4-erster-start-und-einrichtung).

### macOS

Nur Macs mit Apple-Chip (M1 oder neuer) und macOS 14 oder neuer. Öffne das Programm **Terminal**
(Programme → Dienstprogramme).

**1. Homebrew** (Paketmanager für den Mac; überspringen, wenn `brew --version` schon etwas anzeigt):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Am Ende zeigt der Installer unter **„Next steps“** zwei oder drei Befehle an, die Homebrew in deinen Pfad eintragen –
kopiere sie und führe sie aus.

**2. Systemprogramme und uv:**

```bash
brew install git ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Danach das Terminal-Fenster schließen und ein neues öffnen.

**3. Clipper herunterladen und starten:**

```bash
git clone https://github.com/antonplatter-spec/clipping-gta-6.git ~/clipping-gta-6
cd ~/clipping-gta-6
./start.sh
```

Statt `./start.sh` im Terminal kannst du künftig auch im Finder im Ordner `clipping-gta-6` die Datei
**`start.command`** doppelklicken; sie öffnet ein Terminal-Fenster mit dem Server.

Hinweise für den Mac:

- Lass den Ordner in deinem Home-Ordner (`~/clipping-gta-6`), nicht unter *Dokumente*, *Schreibtisch* oder
  *Downloads*: Diese Ordner schützt macOS vor Hintergrundprogrammen, und der [Autostart](#8-dauerbetrieb-autostart)
  könnte dort nicht hinein.
- Die Ersatzstimme fürs Voiceover (`say`) ist in macOS eingebaut.
- Optional erkennt **WhisperKit**, wer in einem Clip spricht (bessere Untertitel bei mehreren Sprechern):
  `brew install whisperkit-cli`.

### Windows mit WSL2

WSL2 ist ein Linux, das Microsoft in Windows mitliefert. Der Clipper läuft darin; das Dashboard öffnest du ganz normal
im Windows-Browser.

**1. WSL2 mit Ubuntu installieren:** Startmenü → „PowerShell“ mit Rechtsklick **als Administrator** öffnen, dann:

```powershell
wsl --install -d Ubuntu
```

Windows neu starten, wenn es danach fragt. Danach öffnet sich das **Ubuntu**-Fenster (sonst: Startmenü → Ubuntu) und
fragt nach einem Linux-Benutzernamen und Passwort – frei wählbar, das Passwort brauchst du für `sudo`.

**2. Im Ubuntu-Fenster** genau die Schritte von [Linux](#linux-ubuntu-debian) ausführen (Systemprogramme, uv,
herunterladen, `./start.sh`).

Wichtig unter WSL:

- Den Clipper **im Linux-Home-Ordner** lassen (`~/clipping-gta-6`, wie oben), nicht unter `/mnt/c/…`: Auf dem
  Windows-Laufwerk ist WSL sehr langsam.
- Das Dashboard im Windows-Browser unter <http://localhost:8765> öffnen. WSL leitet diese Adresse an den Server weiter.
- Der Server läuft nur, solange sein Ubuntu-Fenster offen ist (oder der [Autostart](#8-dauerbetrieb-autostart) ihn
  startet).

---

## 4. Erster Start und Einrichtung

### Was `./start.sh` macht

Beim ersten Mal installiert es alle Python-Pakete (und, falls auf dem PC keins in passender Version ist, Python selbst)
in den Ordner `.venv` im Clipper-Ordner: etwa 120 MB Download, je nach Leitung ein paar Minuten. Danach startet der
Server. Im Terminal steht dann (der Code ist bei dir ein anderer):

```text
GTA Clipper – Automatik 0.1.0
  Dashboard: http://localhost:8765
  Daten:     /home/du/clipping-gta-6/data
  Log:       /home/du/clipping-gta-6/logs/automate/server.log

  Einrichtung: http://localhost:8765/#/einrichtung?token=HWC4-Z76K-BPWZ-IIJ4-3FES
  Einrichtungs-Code: HWC4-Z76K-BPWZ-IIJ4-3FES
  (Der Code gilt bis zum Abschluss der Einrichtung; nach einem Neustart gilt ein neuer.)
  Beenden mit Strg+C.
```

Der **Einrichtungs-Code** sorgt dafür, dass nur du – wer dieses Terminal sieht – das Konto anlegen kann. Auf einem
Rechner mit Bildschirm öffnet sich der Browser von selbst mit dem Link hinter *Einrichtung:*, der Code ist dann schon
eingetragen. Sonst öffne diesen Link selbst (oder <http://localhost:8765> und tippe den Code ab). Sobald die Einrichtung
abgeschlossen ist, zeigt das Terminal keinen Code mehr. Jeder weitere Start dauert nur Sekunden (`start.sh` prüft die
Pakete jedes Mal kurz und bringt sie nach einem Update auf den neuen Stand).

**Das Terminal-Fenster ist der Server.** Solange es offen ist, läuft der Clipper. Mit **Strg+C** beendest du ihn. Damit
er ohne offenes Fenster und nach einem Neustart des PCs läuft, richte den [Autostart](#8-dauerbetrieb-autostart) ein.

### Der Einrichtungsassistent

Beim ersten Öffnen erscheint der Assistent mit sieben Schritten. Schritt 1 und 2 sind Pflicht; alle weiteren kannst
du mit **Überspringen** auslassen und später nachholen (*System → Einrichtungsassistent*).

#### Schritt 1: Konto

Das Feld **Einrichtungs-Code** ist schon ausgefüllt, wenn du den Link aus dem Terminal geöffnet hast; sonst den Code
von dort abtippen. Dazu Benutzername und Passwort (mindestens 10 Zeichen; am besten ein Satz oder ein Passwort aus dem
Passwort-Manager), dann **Konto anlegen**. Es gibt genau ein Konto – deins.

#### Schritt 2: 2FA

Öffne die Authenticator-App auf dem Handy, scanne den QR-Code (oder tippe den angezeigten Schlüssel ab) und gib den
6-stelligen Code aus der App ein: **Code bestätigen**. Ab jetzt brauchst du bei jeder Anmeldung Passwort *und* Code.

#### Schritt 3: Wiederherstellungscodes

Zehn Codes, mit denen du hineinkommst, falls das Handy weg ist. **Sie werden nur jetzt angezeigt.** Speichere sie mit
**Alle kopieren** (z. B. in deinen Passwort-Manager) oder **Als Datei herunterladen**, setze den Haken
*„Ich habe die Codes sicher gespeichert.“* und klicke **Weiter**. Jeder Code funktioniert genau einmal.

#### Schritt 4: Schlüssel

**Anthropic (Claude API) – Pflicht für die Automatik.** Claude wählt damit die Momente, schreibt Hooks und Titel und
übersetzt Dubs.

1. <https://platform.claude.com> öffnen und anmelden (oder ein Konto anlegen).
2. Unter *Billing* Guthaben aufladen: Die API wird nach Verbrauch bezahlt und ist von einem Claude-Abo getrennt.
3. *API Keys* → **Create Key**. Der Schlüssel beginnt mit `sk-ant-`.
4. Im Assistenten einfügen und **Speichern & testen** klicken.

Was es kostet: Der Clipper nutzt standardmäßig **Claude Sonnet 5.5** und rechnet mit den Preisen, die unter
*Einstellungen → Claude → Erweitert* hinterlegt sind (Sonnet 5.5: 2 US-Dollar je Million Eingabe-Tokens, 10 US-Dollar
je Million Ausgabe-Tokens – gleiche das mit Anthropics aktueller Preisliste ab). Antwortet einmal ein Modell, für das
dort kein Preis steht (etwa ein Ersatzmodell, auf das Anthropic ausweicht), meldet das Dashboard das als Alarm. Jede
Anfrage wird mit Tokens und Kosten unter **Kosten & Schlüssel** angezeigt, dort siehst du nach den ersten Läufen, was
ein Video kostet. Zwei Grenzen schützen dich, beide unter *Kosten & Schlüssel → Limits*:

- **Tageslimit** (anfangs **5 €**, 0 = kein Limit): Neue Auswahlen starten nur, solange es nicht erreicht ist; danach
  warten sie bis zum nächsten Tag (Jobs zeigen *„Wartet auf Budget“*). Eine Auswahl, die schon läuft, wird nicht
  abgebrochen – sonst wäre das schon Bezahlte verloren.
- **Höchstens pro Auswahl** (anfangs **2 €**): die Grenze für alle Anfragen einer Auswahl zusammen, auch über
  Neustarts hinweg. Kurz davor wird Claude gebeten, abzugeben; ist sie erreicht, endet die Auswahl als fehlgeschlagen.

Laufende Auswahlen halten sich den Rest ihrer Grenze frei; eine neue startet nur, wenn danach noch Platz bis zum
Tageslimit ist. Ein Tag kostet deshalb höchstens etwa Tageslimit plus eine Auswahl (mit den Standardwerten rund
7 €, dazu höchstens zwei kleine Anfragen einer laufenden Dub-Übersetzung).

Ohne Anthropic-Schlüssel findet und analysiert der Server trotzdem Videos, die Jobs bleiben aber vor der Auswahl
stehen (*„Wartet auf API-Schlüssel“*) und laufen weiter, sobald du den Schlüssel einträgst.

**Fish Audio – optional.** Für KI-Voiceover und für Dubs in andere Sprachen mit der geklonten Stimme des Streamers.

1. <https://fish.audio/app/api-keys/> öffnen und anmelden.
2. Neuen API-Schlüssel erstellen (laut Dashboard reicht die kostenlose Entwickler-Stufe; die Bedingungen stehen bei
   Fish Audio).
3. Einfügen und **Speichern & testen**.

Ohne Fish-Schlüssel spricht die Systemstimme das Voiceover (Linux: espeak-ng, Mac: `say`); Dubs gehen nur mit
Schlüssel.

Beide Schlüssel kannst du jederzeit unter **Kosten & Schlüssel** ändern, testen oder löschen.

#### Schritt 5: Systemcheck

Die Liste zeigt, welche Programme auf dem PC gefunden wurden. Wenn du die Installation oben befolgt hast, sind alle
Pflicht-Werkzeuge grün: `ffmpeg`, `ffprobe`, `yt-dlp`, `parakeet`, `python-pakete`, `python`. Als *optional · fehlt*
dürfen dastehen:

- `demucs` – Stimmen-Trennung, siehe [unten](#stimmen-trennung-demucs-optional),
- `whisperkit` – nur auf dem Mac (`brew install whisperkit-cli`),
- `espeak-ng` bzw. `say` – die Ersatzstimme (unter Linux mit `sudo apt install espeak-ng`).

Fehlt ein Pflicht-Werkzeug, steht der Befehl zum Installieren direkt daneben. Nach der Installation **Erneut prüfen**.
Unter der Liste steht außerdem, ob das Parakeet-Sprachmodell schon heruntergeladen ist; wenn nicht, lädt es die erste
Analyse (rund 650 MB, siehe [Fehlersuche](#13-fehlersuche)).

#### Schritt 6: Quellen

Die Creator (YouTube-Uploads) und Live-Streams aus dem Repo sind schon übernommen. Mit dem Schalter pausierst du eine
Quelle; ein Stream, der gerade aufgenommen wird, hört dann sofort auf (das angefangene Stück wird gespeichert, aber nicht
mehr geclippt – genauso beim Entfernen). Neue Creator und Streams fügst du später unter **Quellen** hinzu (*Creator
hinzufügen*, *Stream hinzufügen*).

#### Schritt 7: Automatik

- **Automatik (tägliche Suche)** – sucht täglich (anfangs um 09:00 Uhr, Zeiten unter *Einstellungen → Zeitplan &
  Automatik*) nach neuen GTA-6-Videos deiner Creator und lässt Claude Clips vorschlagen. Schaltest du sie ein, wenn die
  heutige Suchzeit schon vorbei ist, startet die verpasste Suche sofort.
- **Live-Aufnahme** – prüft alle paar Minuten, ob deine Streams live GTA 6 spielen, nimmt sie auf und clippt jeden
  fertigen 30-Minuten-Abschnitt. Ausschalten beendet laufende Aufnahmen sofort.

Mit **Fertig – zur Übersicht** ist die Einrichtung abgeschlossen. Beide Schalter findest du auch auf der Übersicht.

### Anmelden

Später meldest du dich mit Benutzername, Passwort und dem 2FA-Code aus der App an. Ist das Handy nicht greifbar:
*„Kein Zugriff auf die App? Wiederherstellungscode verwenden“*.

### Stimmen-Trennung (Demucs, optional)

Demucs trennt die Stimmen vom Spielsound: Die Untertitel werden sauberer, und Dubs brauchen es. Es bringt PyTorch mit
– unter Linux rund **3 GB Download** (gut 5 GB installiert), auf dem Mac rund 250 MB. Installieren: den Server mit
**Strg+C** beenden und einmal so starten:

```bash
./start.sh --stimmen
```

Ab dann behält jedes `./start.sh` die Stimmen-Trennung von selbst. Wieder entfernen: `./start.sh --ohne-stimmen`.

Gut zu wissen: Das Modell lädt Demucs beim ersten Clip aus dem Internet. Unter Linux nutzt es eine NVIDIA-Grafikkarte,
wenn `nvidia-smi` vorhanden ist, sonst die CPU (langsamer, funktioniert aber). Führe nicht von Hand `uv sync` ohne
`--extra stimmen` aus – das würde Demucs wieder entfernen; `./start.sh` macht es richtig.

---

## 5. Tägliche Bedienung

- **Übersicht** – Status, *Bereit zum Start*-Liste, Alarme, die nächste Suche. **Jetzt nach neuen Videos suchen**
  startet eine Suche sofort; unter *Video-URL direkt clippen* gibst du ein einzelnes Video ein (**Clippen**).
- **Freigabe** – Claudes Vorschläge: Hooks und Titel ansehen und ändern, dann **Freigeben → Render** oder
  **Ablehnen**. Wenn alles ohne dich gerendert werden soll: *Einstellungen → Freigabe → Automatisch*. Dieselben
  Szenen werden nie zweimal gerendert: Nutzt ein Clip Szenen, die schon ein anderer (fertiger, freigegebener oder
  älterer offener) Clip nutzt, lässt er sich nicht freigeben, bis du einen der beiden ablehnst oder Start/Ende änderst.
  Stellt sich das erst beim Rendern heraus, kommt der Clip mit Begründung in die Freigabe zurück (und ein Alarm sagt es).
- **Videos** – die fertigen Shorts, je Hook eine Variante. **Titel kopieren**, MP4 herunterladen,
  **Als gepostet markieren** oder **Hochladen** (auf ein verbundenes YouTube- oder Instagram-Konto). Ist die Variante
  auf dem gewählten Konto schon online, fragt das Dashboard vor einem zweiten Upload nach; das Abzeichen beim Video
  verlinkt das YouTube-Video bzw. den Reel.
- **Jobs** – was gerade läuft und was gescheitert ist (mit **Erneut versuchen**).
- **Kosten & Schlüssel** – Claude-Kosten pro Tag in Euro, **Tageslimit** und **Höchstens pro Auswahl**,
  API-Schlüssel.
- **Quellen**, **Einstellungen**, **Konten**, **System** – alles Weitere.

---

## 6. YouTube verbinden (optional)

Damit der Clipper Shorts direkt hochlädt, braucht er Zugriff auf deinen Kanal. Dafür legst du einmalig einen eigenen,
kostenlosen „OAuth-Client“ bei Google an (etwa 10 Minuten). Ohne das funktioniert alles andere genauso: Du lädst die
MP4 dann selbst hoch und markierst sie als gepostet.

Öffne im Dashboard **Konten**. Im Kasten *YouTube verbinden* stehen dieselben Schritte und die **Weiterleitungs-URI**,
die du gleich brauchst. Die Menünamen bei Google ändern sich gelegentlich; sinngemäß:

1. <https://console.cloud.google.com/projectcreate> öffnen und ein neues Projekt anlegen (Name z. B. „GTA Clipper“).
   Achte darauf, dass oben dieses Projekt ausgewählt ist.
2. **APIs & Dienste → Bibliothek** → nach **YouTube Data API v3** suchen → **Aktivieren**.
3. **APIs & Dienste → OAuth-Zustimmungsbildschirm** (heißt teils **Google Auth Platform**): Nutzertyp **Extern**,
   App-Name (z. B. „GTA Clipper“) und deine E-Mail-Adresse eintragen. Unter **Zielgruppe → Testnutzer** das
   Google-Konto hinzufügen, dem der YouTube-Kanal gehört.
4. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID** (bzw. *Clients → Client erstellen*):
   Anwendungstyp **Webanwendung**.
5. Bei **Autorisierte Weiterleitungs-URIs** genau die Adresse aus dem Dashboard eintragen (Kopier-Knopf daneben). Bei
   Port 8765 ist das `http://localhost:8765/api/social/youtube/callback`. Startest du den Server mit einem anderen
   Port, ändert sich die Adresse – dann auch bei Google anpassen.
6. **Erstellen**, dann **Client-ID** und **Clientschlüssel** kopieren und im Dashboard bei *Client-ID* und
   *Client-Secret* einfügen → **Speichern**.
7. **Am PC selbst** (nicht vom Handy – Google schickt dich zu `localhost` zurück, also zu diesem PC) auf **Mit YouTube
   verbinden** klicken, das Google-Konto und den Kanal wählen. Google warnt *„Google hat diese App nicht überprüft“*:
   Das ist deine eigene App – über *Erweitert* weiter zur App. Alle Häkchen setzen (besonders *Videos hochladen*) und
   zulassen. Das Dashboard meldet *„YouTube ist verbunden.“*

Hochladen: **Videos** → Variante → **Hochladen**, YouTube-Konto und Sichtbarkeit (privat, nicht gelistet,
öffentlich) wählen.

**Grenzen von Googles Seite** – das kann ich von hier nicht nachprüfen; laut Google, bitte dort prüfen:

- **Uploads bleiben privat:** Videos, die über ein *nicht geprüftes* API-Projekt hochgeladen werden, setzt YouTube auf
  „privat“, bis das Projekt eine Prüfung (Audit) durch YouTube bestanden hat – egal welche Sichtbarkeit du wählst.
  Du kannst sie in YouTube Studio von Hand auf öffentlich stellen oder die Prüfung bei YouTube beantragen.
- **Verbindung läuft nach 7 Tagen ab:** Solange die App im Zustimmungsbildschirm im Status **Testen** ist, verfallen
  Googles Zugangs-Tokens nach 7 Tagen. Das Dashboard meldet dann, dass das Konto neu verbunden werden muss
  (*Konten → Verbinden*). Abhilfe: Unter *Zielgruppe* auf **In Produktion** umstellen (*App veröffentlichen*). Die App
  bleibt „nicht überprüft“, für deinen eigenen Kanal reicht das.
- **Tageskontingent:** Ein Google-Projekt hat standardmäßig 10.000 Einheiten pro Tag, ein Upload kostet viel davon
  (in Googles Doku 1.600 Einheiten, also etwa 6 Uploads pro Tag). Das Kontingent wird um Mitternacht pazifischer Zeit
  zurückgesetzt; ist es aufgebraucht, sagt das Dashboard das beim Upload.

---

## 7. Instagram verbinden (optional)

Damit der Clipper Reels direkt auf Instagram veröffentlicht, braucht er Zugriff auf dein Instagram-Konto – über Metas
offizielle Schnittstelle (Instagram Graph API), ohne dein Passwort. Dafür legst du einmalig eine eigene, kostenlose
„Meta-App“ an (etwa 15 Minuten). Ohne das geht alles andere genauso: MP4 herunterladen, selbst in Instagram posten
und als gepostet markieren.

**Ehrlich vorweg:** Diese Anbindung ist nach Metas Unterlagen gebaut und nur gegen eine Nachbildung von Instagram
getestet – mit einem echten Konto ließ sie sich bisher nicht ausprobieren. Metas Menüs und Regeln ändern sich oft. Wo
„laut Meta, bitte dort prüfen“ steht, konnte ich die Angabe nicht selbst bei Meta nachlesen.

**Voraussetzung:** Dein Instagram-Konto ist ein **professionelles Konto** (Creator oder Business). Umstellen in der
Instagram-App unter *Einstellungen → Kontotyp und Tools → Zu professionellem Konto wechseln* (kostenlos). Private
Konten können über die Schnittstelle nicht posten.

Es gibt zwei Wege. Im Dashboard unter **Konten → Instagram verbinden** stehen dieselben Schritte.

**Weg A: über eine Facebook-Seite (empfohlen).** Laut Metas Anleitung dürfen nur Apps mit Facebook-Login ein Video
direkt vom eigenen PC hochladen (ohne öffentliche Internetadresse) – dieser Weg passt deshalb zu einem privaten PC.

1. Dein Instagram-Konto mit einer **Facebook-Seite** verknüpfen (eine Seite legst du bei Facebook kostenlos an): in
   der Instagram-App über die Kontenübersicht oder auf Facebook in den Einstellungen der Seite unter *Verknüpfte
   Konten* (laut Meta, bitte dort prüfen).
2. <https://developers.facebook.com/apps> öffnen (mit deinem Facebook-Konto; beim ersten Mal registrierst du dich als
   Entwickler) → **App erstellen** → als Typ **Business** bzw. den Anwendungsfall für Instagram-Inhalte wählen, Name
   z. B. „GTA Clipper“ (laut Meta, bitte dort prüfen).
3. In der App das Produkt **Instagram** hinzufügen und **API-Einrichtung mit Facebook-Login** wählen. Die App darf im
   **Entwicklungsmodus** bleiben: Für dein eigenes Konto – du bist Admin der App – braucht es keine App-Prüfung
   (laut Meta, bitte dort prüfen).
4. **App-Einstellungen → Allgemeines**: **App-ID** und **App-Geheimcode** kopieren, im Dashboard unter *Weg A*
   eintragen → **Speichern**. Damit macht der Server aus deinem Token eines, das nicht nach einer Stunde abläuft.
5. Den **Graph API Explorer** öffnen (<https://developers.facebook.com/tools/explorer/>): oben deine App und
   **Benutzer-Token** wählen, die Berechtigungen `instagram_basic`, `instagram_content_publish`, `pages_show_list`
   und `pages_read_engagement` hinzufügen (gehört die Seite zu einem Business-Portfolio, zusätzlich
   `business_management`) → **Generate Access Token** → im Meta-Fenster deine Seite und dein Instagram-Konto
   auswählen und erlauben.
6. Das Token (beginnt mit `EAA`) kopieren, im Dashboard bei **Token einfügen** einsetzen → **Token prüfen und
   verbinden**. Der Server prüft es bei Meta, holt daraus das Seiten-Token (das laut Meta nicht abläuft) und legt für
   jedes gefundene Instagram-Konto ein Konto an. Einmal am Tag fragt er nach, ob es noch gilt.

**Weg B: nur Instagram (ohne Facebook-Seite).** Einfacher einzurichten – aber laut Metas Anleitung ist das direkte
Hochladen einer Videodatei für diesen Weg nicht vorgesehen. Der Server versucht es trotzdem; lehnt Meta ab, steht die
Antwort beim Upload-Job unter **Jobs**, und Weg A ist die Lösung.

1. <https://developers.facebook.com/apps> → **App erstellen** mit dem Anwendungsfall für Instagram bzw. dem Produkt
   **Instagram → API-Einrichtung mit Instagram-Login** (laut Meta, bitte dort prüfen).
2. **App-Rollen → Rollen**: dein Instagram-Konto als **Instagram-Tester** einladen und die Einladung in Instagram
   annehmen (unter *Apps und Websites → Tester-Einladungen*; laut Meta, bitte dort prüfen).
3. Bei **API-Einrichtung mit Instagram-Login → Zugriffstoken generieren** dein Konto hinzufügen, anmelden, das Token
   (beginnt mit `IG`) kopieren und im Dashboard bei **Token einfügen** einsetzen → **Token prüfen und verbinden**.

Ein Token aus Weg B gilt 60 Tage. Der Server erneuert es selbst – zum ersten Mal einen Tag nach dem Einfügen, danach
jede Woche (laut Meta geht das, sobald es 24 Stunden alt ist). Scheitert das, versucht er es frühestens 6 Stunden
später wieder, behält solange das alte Token und meldet sich wenige Tage vor dem Ablauf in der **Übersicht**.

**Mit Instagram anmelden statt Token einfügen:** Das geht nur, wenn dein Server unter einer eigenen **HTTPS-Adresse**
erreichbar ist (`./start.sh --public-url https://…`, fortgeschritten): Meta nimmt als Weiterleitung nach allen Quellen,
die ich prüfen konnte, keine `http://localhost`-Adresse an (laut Meta, bitte dort prüfen). Dann stehen im Kasten unter
*Weg B → Statt Token: mit Instagram anmelden* die Weiterleitungs-URI für die Meta-App und Felder für
**Instagram-App-ID** und **Instagram-App-Geheimcode** (aus *API-Einrichtung mit Instagram-Login*). Für einen privaten
PC ist das Token-Einfügen der einfachere Weg.

**Posten:** **Videos** → Variante → **Hochladen** → Instagram-Konto wählen. **Auch im Feed zeigen** (Standard: an)
zeigt den Reel zusätzlich im Profil-Raster und im Feed. Darunter steht der **Text des Reels**: Titel, Beschreibung mit
Quellenangabe und die Hashtags (ohne YouTubes `#Shorts`; höchstens 2.200 Zeichen und 30 Hashtags). Würde Instagram die
Datei nicht annehmen (zu kurz, falsches Format …), sagt der Dialog das, bevor etwas hochgeladen wird. Nach dem
Hochladen verarbeitet Instagram das Video meist einige Minuten (*„Instagram verarbeitet das Video …“* beim Job);
danach verlinkt das Dashboard den Reel. Ein Upload lässt sich abbrechen, bis die Veröffentlichung an Instagram
geschickt ist. Stürzt der Server mittendrin ab oder reißt die Verbindung ab, macht der nächste Versuch beim selben
Instagram-Container weiter und prüft zuerst, ob der Reel schon online ist – gepostet wird er nur einmal.

**Grenzen von Metas Seite** – laut Meta, bitte dort prüfen:

- Höchstens 100 Beiträge pro Konto in 24 Stunden über die Schnittstelle; ist das erreicht, sagt es der Upload.
- Reels: MP4 mit H.264 und AAC, 3 Sekunden bis 15 Minuten, höchstens 300 MB – die Shorts des Clippers passen.
- Der Clipper spricht mit Version v26.0 der Graph API. Meta schaltet alte Versionen nach etwa zwei Jahren ab; dann
  braucht es ein Clipper-Update.

---

## 8. Dauerbetrieb (Autostart)

Damit der Server „rund um die Uhr clippt“, ohne dass ein Terminal offen sein muss, und nach einem Neustart von selbst
wieder läuft. Am besten erst nach dem Einrichtungsassistenten einrichten (der Server läuft dann im Hintergrund, und
den Einrichtungs-Code im Terminal siehst du nicht mehr). Einmal einrichten:

```bash
cd ~/clipping-gta-6
uv run python -m automate.autostart install
```

Weitere Befehle:

```bash
uv run python -m automate.autostart status     # eingerichtet? läuft er?
uv run python -m automate.autostart restart    # neu starten, z. B. nach einem Update
uv run python -m automate.autostart remove     # Autostart entfernen und den Server beenden
```

Läuft der Server gerade noch in einem Terminal, beende ihn dort mit Strg+C – unter Linux und macOS übernimmt der
Autostart dann innerhalb von 30 Sekunden. Starte danach nicht zusätzlich `./start.sh`: Es kann immer nur ein Server
laufen.

Was `install` je nach System einrichtet:

- **Linux:** einen systemd-Benutzerdienst `gta-clipper`. Er startet beim Hochfahren und nach einem Absturz neu.
  Damit er auch läuft, ohne dass du angemeldet bist, schaltet `install` „Linger“ ein; klappt das nicht ohne
  Administratorrechte, steht der Befehl dafür in der Ausgabe (`sudo loginctl enable-linger DEINNAME`).
  Live-Log: `journalctl --user -u gta-clipper -f`.
- **macOS:** einen LaunchAgent. Er startet, sobald du angemeldet bist, und nach einem Absturz neu. Der Mac muss
  angemeldet und wach bleiben (*Systemeinstellungen → Energie*: automatischen Ruhezustand verhindern). Log:
  `logs/automate/launchd.log`.
- **Windows (WSL2):** WSL läuft nur, solange Windows es gestartet hält. `install` legt deshalb die Datei
  *GTA Clipper.cmd* in den Windows-Autostart-Ordner: Bei jeder Windows-Anmeldung öffnet sich ein minimiertes Fenster
  „GTA Clipper“, das den Server startet. **Dieses Fenster offen lassen** – Schließen beendet den Server. Für den
  Dauerbetrieb darf Windows nicht in den Energiesparmodus gehen (*Einstellungen → System → Netzbetrieb*). Findet
  `install` den Autostart-Ordner nicht, zeigt es den Inhalt der Datei an, die du dann selbst anlegst
  (Windows-Taste + R → `shell:startup`). Bis zur nächsten Anmeldung startest du den Server wie gewohnt mit `./start.sh`.

`install` noch einmal aufrufen (z. B. mit anderen Optionen, siehe [Handy-Zugriff](#9-zugriff-vom-handy-im-heimnetz))
ersetzt die alte Einrichtung. Ein Neustart des Servers ist harmlos: Laufende Live-Aufnahmen nehmen weiter auf, und
unterbrochene Jobs macht der Server danach weiter – eine unterbrochene Claude-Auswahl an der Stelle, an der sie war
(höchstens eine Anfrage wird wiederholt). Damit ein Job, der immer wieder unterbrochen wird, nicht endlos wiederholt
und bezahlt wird, zählt jede Unterbrechung einer Claude-Auswahl und jeder Absturz als Versuch; nach dem dritten steht
der Job als fehlgeschlagen unter **Jobs**, und **Erneut versuchen** setzt ihn fort. Nach dem Start und nach dem
Aufwachen aus dem Ruhezustand wartet die Suche 2 Minuten, bis das Netzwerk steht; erreicht eine geplante Suche keine
einzige Quelle, versucht sie es alle 15 Minuten erneut (bis zu zwei Stunden lang).

**Hast du früher `/clip-schedule` oder `clipper schedule` benutzt?** Diese alten Zeitpläne starten Claude Code und
einen eigenen Live-Watcher und dürfen nicht neben dem Server laufen (doppelte Arbeit und Kosten). Entfernen:

```bash
uv run python -m clipper schedule remove
```

Geplante Aufgaben, die du in der Claude-Code-Desktop-App oder der Codex-App angelegt hast (z. B. `gta6-daily-clips`),
löschst du dort – die kann der Server nicht sehen. `autostart install` und `autostart status` warnen, solange alte
`clipper schedule`-Zeitpläne gefunden werden, und das Dashboard zeigt dann einen Alarm.

---

## 9. Zugriff vom Handy im Heimnetz

Normalerweise ist das Dashboard nur auf dem PC selbst erreichbar. Fürs Handy im selben WLAN:

```bash
./start.sh --host 0.0.0.0
```

(mit Autostart: `uv run python -m automate.autostart install --host 0.0.0.0`). Das Terminal zeigt dann eine Warnung
und die Adressen, unter denen der Server im Netz erreichbar ist, z. B. `http://192.168.178.20:8765` – die öffnest du
auf dem Handy.

Was das bedeutet:

- Jeder im selben WLAN/LAN kann die **Anmeldeseite** öffnen. Geschützt ist alles durch Passwort und 2FA.
- Die Verbindung ist **unverschlüsselt (HTTP)**. Im eigenen, vertrauenswürdigen Heimnetz ist das vertretbar, in einem
  fremden oder öffentlichen WLAN nicht.
- **Niemals** im Router eine Portweiterleitung auf diesen Port einrichten – der Server ist nicht dafür gebaut, offen
  im Internet zu stehen.
- *YouTube verbinden* geht trotzdem nur am PC selbst. Ein Instagram-Token einfügen geht auch vom Handy.
- Fragt macOS, ob Python eingehende Verbindungen annehmen darf: erlauben. Unter Ubuntu mit aktiver Firewall (ufw):
  `sudo ufw allow 8765/tcp`.
- Unter **WSL2** ist der Server so nicht vom Handy erreichbar (WSL hängt hinter Windows' eigenem Netz). Das lässt sich
  mit dem „gespiegelten Netzwerkmodus“ von WSL ändern – das ist fortgeschritten und hier nicht beschrieben.

---

## 10. Aktualisieren

Deine Daten (`data/`, `exports/`, `jobs/`, Logs) liegen nicht in Git und bleiben bei einem Update unberührt, ebenso
alle Einstellungen im Dashboard.

**Ohne Autostart:** Server mit Strg+C beenden, dann:

```bash
cd ~/clipping-gta-6
git checkout -- uv.lock
git pull
./start.sh
```

**Mit Autostart** (Linux, macOS):

```bash
cd ~/clipping-gta-6
git checkout -- uv.lock
git pull
uv run python -m automate.autostart restart
```

Unter WSL mit Autostart: `git checkout -- uv.lock` und `git pull` im Ubuntu-Fenster, dann das Fenster „GTA Clipper“
schließen und `./start.sh` starten (oder dich bei Windows ab- und wieder anmelden).

`git checkout -- uv.lock` verwirft nur ein eventuelles yt-dlp-Notfall-Update (siehe unten), damit `git pull` nicht
daran hängen bleibt; sonst tut es nichts. Beim Start bringt `start.sh` die Python-Pakete auf den Stand des Updates.

**yt-dlp schnell aktualisieren:** YouTube ändert sich oft, und yt-dlp (das Programm, das Videos lädt) muss dann
nachziehen. Scheitern Downloads plötzlich, hilft meist schon ein normales Update (oben). Ist dort noch keine neue
Version drin:

```bash
cd ~/clipping-gta-6
uv lock --upgrade-package yt-dlp
```

und den Server neu starten (Strg+C und `./start.sh`, bzw. `uv run python -m automate.autostart restart`).

---

## 11. Datensicherung

Was wo liegt (alles im Clipper-Ordner):

| Ordner/Datei | Inhalt |
| --- | --- |
| `data/automate.db` | Die Datenbank: dein Login, Quellen, Einstellungen, Jobs, Picks, die Liste der Videos, Kosten – **und alle Schlüssel** |
| `exports/` | Die fertigen Shorts mit Titeln und Beschreibungen |
| `jobs/` | Arbeitsdateien: Transkripte und Picks bleiben, große Quellvideos werden automatisch gelöscht |
| `assets/music/local/` | Musik, die du im Dashboard freigegeben hast |
| `logs/` | Protokolle |

**Ehrlich zu den Schlüsseln:** Der Anthropic-Schlüssel, der Fish-Audio-Schlüssel, Client-ID und Client-Secret von
Google, App-IDs und App-Geheimcodes deiner Meta-App sowie die YouTube- und Instagram-Zugangstokens stehen **im
Klartext** in `data/automate.db`. Geschützt sind sie nur durch die
Dateirechte: Die Datei und der Ordner `data/` sind nur für deinen Benutzer lesbar. Wer an deinen Benutzer-Account,
an die Festplatte oder an eine Kopie der Datei kommt, kann sie lesen. Dasselbe gilt für das 2FA-Geheimnis (der
Server braucht es lesbar, um Codes zu prüfen). Nur dein Passwort und die Wiederherstellungscodes sind als Hash
gespeichert, also nicht zurückzurechnen.

Darum:

- Sicherungen von `data/` **verschlüsselt** aufbewahren (z. B. verschlüsseltes Backup-Laufwerk), nicht in einen
  geteilten Cloud-Ordner legen.
- Gerät verkauft oder eine Kopie verloren? Schlüssel beim Anbieter widerrufen: Anthropic-Schlüssel in der Claude
  Console löschen, Fish-Schlüssel bei Fish Audio, in der Google Cloud Console ein neues Client-Secret erzeugen und
  unter <https://myaccount.google.com/permissions> den Zugriff der App entfernen; bei Meta in der App einen neuen
  App-Geheimcode erzeugen und den Zugriff der App entfernen (Facebook: *Einstellungen → Business-Integrationen*,
  Instagram: *Apps und Websites*; laut Meta, bitte dort prüfen).

**Sichern:** Dieser Befehl kopiert die Datenbank in einem stimmigen Zustand nach `sicherung.db` – auch während der
Server läuft (Autostart inklusive):

```bash
cd ~/clipping-gta-6
uv run python -c "import sqlite3; d = sqlite3.connect('sicherung.db'); sqlite3.connect('data/automate.db').backup(d); d.close()"
chmod 600 sicherung.db
```

Dann `sicherung.db` und den Ordner `exports/` an einen sicheren Ort kopieren. (Einfach `data/automate.db` zu kopieren,
während der Server läuft, kann eine halbfertige Datei ergeben; bei beendetem Server geht auch das.)

**Zurückspielen:** Server beenden (Strg+C, mit Autostart `uv run python -m automate.autostart remove`), die Sicherung
als `data/automate.db` hineinlegen, Server wieder starten (`./start.sh` bzw. `… autostart install`).

---

## 12. Zugang verloren

- **Handy weg, Passwort bekannt:** Bei der Anmeldung *„Kein Zugriff auf die App? Wiederherstellungscode verwenden“*
  und einen der gespeicherten Codes eingeben. Danach unter **System → 2FA neu einrichten** das neue Handy verbinden.
- **Passwort vergessen oder keine Codes mehr:** Am PC selbst im Terminal:

  ```bash
  cd ~/clipping-gta-6
  uv run python -m automate reset-login
  ```

  Zum Bestätigen `RESET` eintippen. Das löscht dein Konto (Passwort, 2FA, Wiederherstellungscodes, Sitzungen);
  Quellen, Videos und Einstellungen bleiben, ebenso die API-Schlüssel und YouTube-/Instagram-Verbindungen (willst du die auch
  löschen, hänge `--wipe-keys` an). Der Befehl zeigt danach einen neuen **Einrichtungs-Code** und den Link
  *Einrichtung:* – öffne ihn und lege das Konto im Assistenten neu an. Der Server muss dafür nicht neu starten. Der
  Zugriff auf das Terminal dieses PCs ist hier der Beweis, dass du der Besitzer bist.

---

## 13. Fehlersuche

**Erster Schritt – der Systemcheck im Terminal:**

```bash
cd ~/clipping-gta-6
uv run python -m automate doctor
```

`✓` = da, `✗` = fehlt und wird gebraucht (darunter steht, wie du es installierst), `–` = optional. Dieselbe Liste
zeigt das Dashboard unter **System**.

**Logs:** `logs/automate/server.log` im Clipper-Ordner (ältere Teile als `.1`–`.3`). Mit Autostart zusätzlich
`journalctl --user -u gta-clipper` (Linux) bzw. `logs/automate/launchd.log` (Mac). Was ein einzelner Job gemacht hat,
steht im Dashboard unter **Jobs** beim jeweiligen Job.

**Häufige Probleme:**

- **`uv fehlt`** – uv installieren (`curl -LsSf https://astral.sh/uv/install.sh | sh`) und ein neues Terminal öffnen.
- **`./start.sh: Permission denied`** – der Ordner kam nicht über `git clone` (z. B. als ZIP). Einmal
  `chmod +x start.sh start.command` ausführen, oder mit `sh start.sh` starten.
- **„Die Python-Pakete ließen sich nicht installieren“** – meist keine Internetverbindung oder zu wenig Speicherplatz;
  die genaue Meldung von uv steht direkt darüber. Danach einfach `./start.sh` noch einmal.
- **„Port 8765 ist schon belegt“** – ein anderes Programm nutzt den Port. Anderen Port nehmen: `./start.sh --port 8766`
  (dann ist das Dashboard unter `http://localhost:8766` und die YouTube-Weiterleitungs-URI ändert sich mit).
- **Einrichtungs-Code fehlt oder wird abgelehnt** (z. B. weil der Server per Autostart läuft und du sein Terminal
  nicht siehst, oder nach einem Neustart ein neuer Code gilt): `uv run python -m automate reset-login` im
  Clipper-Ordner zeigt, solange noch kein Konto eingerichtet ist, sofort einen neuen Code samt Link.
- **„Der Server läuft schon für diesen Datenordner …“** – es läuft bereits ein Server (oft der Autostart). Die Meldung
  nennt die Adresse; öffne das Dashboard dort oder beende den anderen Server zuerst.
- **„Unbekannte Adresse. Öffne das Dashboard über http://localhost:8765“** – du hast eine Adresse benutzt, die der
  Server nicht kennt (z. B. die IP-Adresse ohne `--host 0.0.0.0`). Nimm die Adresse, die im Terminal steht.
- **Downloads scheitern (yt-dlp)** – bei *„Sign in to confirm you're not a bot“* blockiert YouTube deine
  IP-Adresse (häufig bei VPNs und Servern im Rechenzentrum); hilft nur Abwarten oder ein anderer Internetanschluss.
  Bei Fehlern wie *„Requested format is not available“* oder Problemen mit YouTubes „Challenges“ yt-dlp aktualisieren
  ([Abschnitt 10](#10-aktualisieren)).
- **Die erste Analyse hängt oder scheitert beim Modell** – beim allerersten Transkribieren lädt der Clipper das
  Parakeet-Modell (etwa 650 MB) von huggingface.co in `~/.cache/huggingface`. Ohne Internet oder wenn eine Firewall
  huggingface.co sperrt, scheitert das. Später läuft alles offline. Ob das Modell schon da ist, steht unter **System**.
  Dasselbe gilt für das Demucs-Modell beim ersten Clip mit Stimmen-Trennung.
- **Alarm „Nur noch … GB frei“** – bis wieder Platz ist, warten neue Downloads (*„Wartet: nur … GB frei“*) und es
  starten keine neuen Live-Aufnahmen. Alte Quellvideos löscht der Server selbst. Hilft das nicht: alte Exporte unter
  `exports/` löschen (vorher sichern) oder die Grenze unter *Einstellungen → Analyse → Alarm unter freiem Speicher*
  senken.
- **Jobs „Wartet auf API-Schlüssel“ / „Wartet auf Budget“** – Anthropic-Schlüssel eintragen bzw. das Tageslimit
  erhöhen (beides unter *Kosten & Schlüssel*). Die Jobs laufen danach von selbst weiter. *„Wartet: das restliche
  Tagesbudget ist für laufende Auswahlen verplant“* löst sich von selbst, sobald diese fertig sind.
- **Auswahl „hat ihr Kostenlimit erreicht“** – Claude hat für diese Quelle mehr gebraucht als *Höchstens pro Auswahl*
  erlaubt. Passiert das öfter, die Grenze unter *Kosten & Schlüssel → Limits* erhöhen; **Erneut versuchen** startet die
  Auswahl neu (und kostet noch einmal).
- **Job „…-mal mitten in der Arbeit unterbrochen“** – der Server wurde während dieses Jobs mehrmals beendet oder ist
  abgestürzt. **Erneut versuchen** setzt ihn fort; passiert es wieder, steht der Grund im Server-Log.
- **Alarm „… wurde nicht gerendert, weil seine Szenen schon ein anderer Clip verwendet“** – der Clip wartet wieder in
  der **Freigabe**: einen der beiden ablehnen oder Start/Ende ändern, dann freigeben.
- **Alarm „Die Suche … hat keine einzige Quelle erreicht“** – meist war beim Suchen kein Internet da; die geplante
  Suche versucht es alle 15 Minuten erneut. Bleibt der Alarm, Internetverbindung prüfen bzw. yt-dlp aktualisieren
  ([Abschnitt 10](#10-aktualisieren)).
- **Voiceover scheitert beim Rendern** – kein Fish-Schlüssel und keine Systemstimme: Fish-Schlüssel eintragen oder
  `sudo apt install espeak-ng`.
- **Der Browser öffnet sich nicht** – auf Rechnern ohne Bildschirm oder unter WSL ohne Hilfsprogramm normal; die
  Adresse von Hand öffnen.
- **Mac: „Dieser Mac meldet sich als Intel (x86_64)“** – entweder ein Intel-Mac (nicht unterstützt) oder das Terminal
  läuft unter Rosetta: Programme → Dienstprogramme → Terminal → *Informationen* → Haken bei *Mit Rosetta öffnen*
  entfernen.
- **Instagram: „Das ist ein privates Instagram-Konto“** – auf ein professionelles Konto umstellen
  ([Abschnitt 7](#7-instagram-verbinden-optional)), dann ein neues Token erzeugen und einfügen.
- **Instagram: „Dem Token fehlen Berechtigungen“ / „Dem Zugriff fehlt eine Berechtigung“** – im Graph API Explorer
  bzw. beim Token-Erzeugen alle Berechtigungen aus Abschnitt 7 setzen, neues Token einfügen.
- **Instagram: „Zu diesem Token gibt es keine Facebook-Seite mit verknüpftem Instagram-Profikonto“** – Instagram-Konto
  und Facebook-Seite verknüpfen und beim Erzeugen des Tokens Seite *und* Instagram-Konto auswählen.
- **Instagram-Upload: „… funktioniert das direkte Hochladen … nur mit einer Meta-App mit Facebook-Login“** – Meta nimmt
  Weg B für Uploads nicht an: Weg A einrichten (Abschnitt 7).
- **Alarm „Instagram-Konto … ist nicht mehr verbunden“ oder „Der Instagram-Zugriff … läuft … ab“** – unter **Konten**
  ein neues Token einfügen; mit **Prüfen** neben dem Konto fragt das Dashboard Meta sofort, ob das Token gilt.
- **Instagram: „Instagram ist nicht erreichbar“** – meist keine Internetverbindung; ein Upload-Job versucht es selbst
  noch einmal (**Jobs**).
- **WSL: <http://localhost:8765> geht im Windows-Browser nicht** – prüfen, ob das Ubuntu-Fenster mit dem Server noch
  offen ist; in PowerShell `wsl --update` ausführen und WSL mit `wsl --shutdown` neu starten.

---

## 14. Deinstallieren

1. Autostart entfernen (beendet auch den Server):

   ```bash
   cd ~/clipping-gta-6
   uv run python -m automate.autostart remove
   ```

   Ohne Autostart: den Server mit Strg+C beenden.
2. Laufende Live-Aufnahmen hören von selbst auf, wenn der Stream endet. Hast du vor Schritt 1 im Dashboard die
   **Live-Aufnahme** ausgeschaltet, läuft keine mehr; sonst sofort beenden: `pkill -f "clipper live record"`.
3. Was du behalten willst, sichern (vor allem `exports/`).
4. Den Ordner löschen: `rm -rf ~/clipping-gta-6`.
5. Optional aufräumen: heruntergeladene Modelle in `~/.cache/huggingface` (nur, wenn kein anderes Programm sie
   braucht), unter Linux `loginctl disable-linger`, und uv selbst (Anleitung auf <https://docs.astral.sh/uv/>).
6. Schlüssel und Zugriffe widerrufen, wenn du sie nicht mehr brauchst (siehe [Datensicherung](#11-datensicherung)).

---

## 15. Rechtliches

Poste nur Clips, für die du die Rechte oder die Erlaubnis des Creators hast. Viele Streamer freuen sich über Clipper,
manche bezahlen sogar über Clipping-Programme. Dieses Projekt steht in keiner Verbindung zu Rockstar Games, Take-Two
oder den gezeigten Streamern.

KI-Dubs tragen in der Beschreibung einen Hinweis, dass es sich um eine KI-Synchronisation handelt; beim Hochladen
über das Dashboard wird bei Dubs zusätzlich YouTubes Kennzeichnung für veränderte/synthetische Inhalte gesetzt. Bei
Instagram setzt das Dashboard keine solche Kennzeichnung (ob und wie das über die Schnittstelle geht, ließ sich nicht
prüfen) – kennzeichne Dubs dort bei Bedarf selbst in der Instagram-App.

Der Clipper steht unter der Lizenz AGPL-3.0 (Datei `LICENSE`).

---

## 16. Alle Befehle auf einen Blick

Alle im Clipper-Ordner (`cd ~/clipping-gta-6`):

| Befehl | Wofür |
| --- | --- |
| `./start.sh` | Server starten (prüft vorher die Python-Pakete) |
| `./start.sh --port 8766` | auf einem anderen Port |
| `./start.sh --host 0.0.0.0` | im Heimnetz erreichbar (Handy) |
| `./start.sh --no-browser` | ohne den Browser zu öffnen |
| `./start.sh --stimmen` / `--ohne-stimmen` | Stimmen-Trennung (Demucs) installieren / entfernen |
| `uv run python -m automate doctor` | Systemcheck im Terminal |
| `uv run python -m automate reset-login` | Login zurücksetzen (Passwort/2FA vergessen); zeigt einen neuen Einrichtungs-Code. Mit `--wipe-keys` auch alle API-Schlüssel löschen und YouTube/Instagram trennen |
| `uv run python -m automate import-yaml` | Creator, Streams und Einstellungen aus den Repo-Dateien erneut übernehmen (fügt nur Fehlendes hinzu) |
| `uv run python -m automate.autostart install` / `status` / `restart` / `remove` | Autostart |
| `uv run python -m clipper schedule remove` | alte `clipper schedule`-Zeitpläne entfernen |
| `uv run python -m clipper bench` | messen, wie viele Live-Streams der PC schafft |
| `uv lock --upgrade-package yt-dlp` | yt-dlp-Notfall-Update (danach neu starten) |
