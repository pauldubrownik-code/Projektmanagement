# Projektmanagement

Kanban + Gantt + Kalender + Routinen + Pomodoro. Zwei Deployment-Varianten liegen im selben Repo:

## 1. GitHub Pages (statisch, mit GitHub-Gist-Sync)

`docs/index.html` ist eine eigenständige, statische Version der App. Sie läuft komplett im
Browser und synchronisiert alle Daten (Aufgaben, Routinen, …) als JSON in einem **privaten
GitHub Gist** unter deinem eigenen Account — dadurch funktioniert sie auf mehreren Geräten
(Desktop + Handy), ganz ohne eigenen Server.

**Einmalige Einrichtung:**

1. **GitHub Pages aktivieren:** Repo → *Settings → Pages* → unter "Build and deployment" →
   Source: *Deploy from a branch* → Branch: `main`, Ordner: `/docs` → Speichern.
   Die Seite ist danach unter `https://<user>.github.io/<repo>/` erreichbar.
2. **Personal Access Token erstellen:** github.com → *Settings → Developer settings →
   Personal access tokens → Tokens (classic)* → *Generate new token* → nur den Scope
   **`gist`** anhaken, sonst nichts.
3. In der App auf **⚙️ Sync** klicken, Token einfügen, speichern. Beim ersten Speichern wird
   automatisch ein neuer privater Gist angelegt.
4. Auf einem zweiten Gerät (z.B. Handy) dieselbe Seite öffnen und **dasselbe Token** eintragen
   → Daten werden synchron gehalten. "🔄 Aktualisieren" holt den aktuellen Stand vom Gist.

**Wichtig:** Das Token liegt nur im `localStorage` dieses Browsers/Geräts, wird an niemanden
außer `api.github.com` gesendet und sollte ausschließlich den `gist`-Scope haben. Über
⚙️ Sync → *Backup exportieren* lässt sich jederzeit eine JSON-Sicherung herunterladen.

## 2. Eigener Server (Render o.ä.)

`server.py` ist ein eigenständiger Python-Server (reine Standardbibliothek, keine
Abhängigkeiten) mit SQLite-Datenbank — für Render vorkonfiguriert (`runtime.txt`,
`PORT`/`HOST` aus Env, optionaler Basic-Auth über `KANBAN_USER`/`KANBAN_PASS`, siehe
`.env.example`). Start lokal: `python3 server.py` → http://localhost:8089

Für dauerhafte Daten auf Render wird ein *Persistent Disk* benötigt (im Free-Tier nicht
verfügbar), da sonst die SQLite-Datei bei jedem Neustart/Deploy verloren geht.

Die beiden Varianten teilen sich keine Daten — GitHub Pages nutzt den Gist, der
Render-Server die SQLite-Datei.
