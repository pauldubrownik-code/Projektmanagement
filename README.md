# Projektmanagement · Christian Radden

Dein persönliches Projektmanagement-Repository.

## Struktur

```
Projektmanagement/
├── README.md              ← diese Datei
├── board/                 ← Aufgaben & Arbeitspakete
│   ├── aktuell.md         ← aktuelle Sprint-Übersicht
│   └── archiv/            ← erledigte Aufgaben
├── meetings/              ← Meeting-Notizen
│   └── template.md        ← Vorlage für Termine
└── dashboard.html         ← generierte Projektübersicht
```

## Projekte (aktuell)

| Projekt | Status | Priorität | Deadline |
|---------|--------|-----------|----------|
| Jessi / Trennung | läuft | 🔴 hoch | — |
| Roman / Ausschreibungstool | läuft | 🔴 hoch | — |
| NGD Tagesgeschäft | läuft | 🔴 hoch | täglich |
| EnnAIgram Shorts | wartet | 🟡 mittel | — |
| Emil Hermes Agent | wartet | 🟡 mittel | — |
| Ken Webb Wiki | erledigt | 🟢 niedrig | ✅ |

## Arbeitsweise

1. **Wochen-Review** (z.B. Montag) – wir gehen alle Projekte durch
2. **Aufgaben definieren** – Arbeitspakete mit Deadline beschreiben
3. **Umsetzung** – ich arbeite Aufgaben ab oder unterstütze dich
4. **Folgetermin** – wir prüfen Fortschritt und passen an
5. **Erinnerungen** – ich erinnere an Fristen und motiviere

## Cron-Jobs

- Täglicher Check-in: `0 8 * * *` – Projekt-Status, Erinnerungen
- Weekly Review: `0 16 * * 1` – Wochenrückblick und Planung
