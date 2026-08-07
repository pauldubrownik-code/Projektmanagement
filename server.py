#!/usr/bin/env python3
"""Interaktives Kanban + Gantt + Brainstorming + Pomodoro — API-Server + Frontend.

Start:  python3 server.py
Dann:   http://localhost:8089
"""

import json
import os
import sqlite3
import uuid
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

BASE = Path(__file__).parent.resolve()
KANBAN_DB = BASE / "data" / "kanban.db"
PORT = int(os.environ.get("PORT", 8089))
HOST = "0.0.0.0"

# ── Optionaler Passwortschutz ──
AUTH_USER = os.environ.get("KANBAN_USER", "")
AUTH_PASS = os.environ.get("KANBAN_PASS", "")
AUTH_REALM = "Projektmanagement"

# ── GitHub Sync für DB-Persistenz ──
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GIT_REMOTE = "origin"
GIT_BRANCH = "main"


def sync_db_to_git():
    """Commit und Push der DB nach jeder Änderung, damit Daten
    bei Render-Neudeployments nicht verloren gehen."""
    if not GITHUB_TOKEN:
        return  # sync deaktiviert
    try:
        cwd = str(BASE)
        _run_git(["git", "add", str(KANBAN_DB.relative_to(BASE))], cwd)
        res = _run_git(["git", "diff", "--cached", "--quiet"], cwd, silent_ok=True)
        if res.returncode == 0:
            return  # nichts geändert
        _run_git(["git", "commit", "-m", "auto-sync DB [ci skip]"], cwd,
                 env_overrides={"GIT_AUTHOR_NAME": "Kanban Bot",
                                "GIT_AUTHOR_EMAIL": "bot@kanban.local",
                                "GIT_COMMITTER_NAME": "Kanban Bot",
                                "GIT_COMMITTER_EMAIL": "bot@kanban.local"})
        r2 = _run_git(["git", "push", "origin", "main"], cwd)
        if r2.returncode == 0:
            _sync_log("DB erfolgreich gepusht ✅")
        else:
            _sync_log(f"Push fehlgeschlagen: {r2.stderr.strip()}")
    except Exception as e:
        _sync_log(f"Fehler: {e}")

PRIO_LABELS = {1: "hoch", 2: "mittel", 3: "niedrig"}
PRIO_VALUES = {"hoch": 1, "mittel": 2, "niedrig": 3}
STATUS_ORDER = ["backlog", "ready", "running", "completed", "blocked"]

# Die DB wird auch vom Hermes-Agenten beschrieben; dessen Statuswerte
# müssen auf die fünf Board-Spalten abgebildet werden, sonst fallen die
# Karten im Frontend aus allen Spalten heraus.
STATUS_ALIASES = {
    "todo": "backlog", "open": "backlog", "new": "backlog",
    "queued": "ready", "pending": "ready",
    "in_progress": "running", "doing": "running", "active": "running", "started": "running",
    "done": "completed", "finished": "completed", "closed": "completed",
    "waiting": "blocked", "on_hold": "blocked", "paused": "blocked",
}


def normalize_status(raw):
    s = str(raw or "").strip().lower()
    if s in STATUS_ORDER:
        return s
    return STATUS_ALIASES.get(s, raw)


def get_db():
    con = sqlite3.connect(str(KANBAN_DB))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _week_start_ts():
    """Return unix timestamp for Monday 00:00 of the current week."""
    import datetime as dt
    now_dt = dt.datetime.now()
    monday = now_dt - dt.timedelta(days=now_dt.weekday())
    return int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _month_start_ts():
    """Return unix timestamp for 1st of current month 00:00."""
    import datetime as dt
    now_dt = dt.datetime.now()
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(month_start.timestamp())


def ensure_tables():
    con = get_db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS brainstorm (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            project TEXT DEFAULT '',
            processed INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS task_estimates (
            task_id TEXT PRIMARY KEY,
            estimated_minutes INTEGER DEFAULT 0,
            buffer_percent INTEGER DEFAULT 20,
            notes TEXT DEFAULT ''
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            status TEXT DEFAULT 'ready',
            priority INTEGER DEFAULT 2,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            assignee TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            procedure TEXT DEFAULT ''
        )
    """)
    # Spalten für bestehende DBs nachrüsten
    for col in ["procedure", "project_ids"]:
        try:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Spalte existiert bereits
    # Migration: project_id → project_ids JSON-Array
    try:
        rows = con.execute("SELECT id, project_id FROM tasks WHERE project_id IS NOT NULL AND project_id != '' AND (project_ids IS NULL OR project_ids = '' OR project_ids = '[]')").fetchall()
        for r in rows:
            con.execute("UPDATE tasks SET project_ids=? WHERE id=?", (json.dumps([r["project_id"]]), r["id"]))
    except sqlite3.OperationalError:
        pass
    con.execute("""
        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT DEFAULT '',
            icon TEXT DEFAULT '🔄',
            sort_order INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS routine_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            completed_at INTEGER NOT NULL
        )
    """)
    # Dashboard: Habits
    con.execute("""CREATE TABLE IF NOT EXISTS habits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        icon TEXT DEFAULT '✅',
        created_at INTEGER DEFAULT (strftime('%s','now'))
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS habit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        habit_id INTEGER NOT NULL,
        log_date TEXT NOT NULL,
        FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
    )""")
    # Dashboard: Weekly Goals
    con.execute("""CREATE TABLE IF NOT EXISTS weekly_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        icon TEXT DEFAULT '🎯',
        target_value REAL DEFAULT 1,
        current_value REAL DEFAULT 0,
        unit TEXT DEFAULT 'x',
        week_start INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT (strftime('%s','now'))
    )""")
    # Dashboard: Revenue
    con.execute("""CREATE TABLE IF NOT EXISTS revenue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        amount REAL NOT NULL,
        month TEXT NOT NULL,
        created_at INTEGER DEFAULT (strftime('%s','now'))
    )""")
    # Dashboard: Important Emails
    con.execute("""CREATE TABLE IF NOT EXISTS important_emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT NOT NULL,
        subject TEXT NOT NULL,
        preview TEXT DEFAULT '',
        priority TEXT DEFAULT 'mittel',
        is_read INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT (strftime('%s','now'))
    )""")

    # KI-Lern-Regeln – lernt aus Benutzereingriffen
    con.execute("""CREATE TABLE IF NOT EXISTS ki_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT UNIQUE NOT NULL,
        project TEXT DEFAULT '',
        priority TEXT DEFAULT '',
        estimated_minutes INTEGER DEFAULT 0,
        corrected_at INTEGER NOT NULL
    )""")
    con.commit()
    con.close()


ensure_tables()

SYNC_LOG = []  # wird von ensure_git_repo() und sync_db_to_git() befüllt


def _sync_log(msg):
    SYNC_LOG.append(msg)
    print(f"[DB-SYNC] {msg}", flush=True)


def _run_git(cmd, cwd, check=False, silent_ok=False, env_overrides=None):
    """Git-Befehl ausführen und bei Fehlern loggen. Mit check=True wird eine Exception geworfen."""
    import subprocess, os
    env = {**os.environ, **(env_overrides or {}), "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env)
    if r.returncode != 0:
        msg = f"{' '.join(cmd)}: {r.stderr.strip() or r.stdout.strip()}"
        if check:
            raise RuntimeError(f"Git fehlgeschlagen: {msg}")
        if not silent_ok:
            _sync_log(f"⚠️ {msg}")
    return r


def ensure_git_repo():
    """Initialisiert ein Git-Repo auf Render (wo Git fehlt) und
    holt den letzten Stand aus GitHub, damit sync_db_to_git() pushen kann."""
    if not GITHUB_TOKEN:
        _sync_log("Kein GITHUB_TOKEN — Git-Sync deaktiviert")
        return
    cwd = str(BASE)
    git_dir = str(BASE / ".git")
    repo_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/pauldubrownik-code/Projektmanagement.git"

    import os, shutil, subprocess

    # Prüfen ob wir einen gültigen Remote haben
    def _has_remote():
        r = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True, cwd=cwd)
        return "origin" in r.stdout

    if not os.path.exists(git_dir) or not _has_remote():
        # Alten Git-Müll wegräumen und frisch starten
        if os.path.exists(git_dir):
            _sync_log("Entferne ungültiges Git-Repo (kein Remote)...")
            shutil.rmtree(git_dir)
        _sync_log("Initialisiere Git-Repo...")
        try:
            _run_git(["git", "init"], cwd, check=True)
            _run_git(["git", "remote", "add", "origin", repo_url], cwd, check=True)
            _run_git(["git", "config", "user.name", "Kanban Bot"], cwd)
            _run_git(["git", "config", "user.email", "bot@kanban.local"], cwd)
        except Exception as e:
            _sync_log(f"Git-Init fehlgeschlagen: {e}")
            return

        # DB vor dem Reset sichern
        db_backup = KANBAN_DB.read_bytes() if KANBAN_DB.exists() else None

        # Versuche von GitHub zu fetchen
        r = _run_git(["git", "fetch", "origin", "main"], cwd, silent_ok=True)
        try:
            if r.returncode == 0:
                _run_git(["git", "checkout", "-f", "-b", "main", "origin/main"], cwd, check=True)
                _sync_log("Von GitHub geholt & eingerichtet ✅")
            else:
                _run_git(["git", "checkout", "-b", "main"], cwd)
                _sync_log("Leeres Repo initiiert (kein Remote-Zugriff) ❌")
        except Exception as e:
            _sync_log(f"Checkout fehlgeschlagen: {e} — initiiere leeres Repo")
            _run_git(["git", "checkout", "-b", "main"], cwd)

        # DB wiederherstellen
        if db_backup:
            KANBAN_DB.parent.mkdir(parents=True, exist_ok=True)
            KANBAN_DB.write_bytes(db_backup)
        ensure_tables()
    else:
        r = _run_git(["git", "pull", "--rebase"], cwd, silent_ok=True)
        if r.returncode == 0:
            _sync_log("Git-Repo aktualisiert ✅")
        else:
            _sync_log(f"Git-Pull fehlgeschlagen: {r.stderr.strip()}")


try:
    ensure_git_repo()
except Exception as e:
    _sync_log(f"ensure_git_repo() komplett fehlgeschlagen: {e}")


def fetch_tasks():
    con = get_db()
    rows = con.execute(
        "SELECT id, title, body, procedure, status, priority, created_at, started_at, "
        "completed_at, assignee, project_ids FROM tasks ORDER BY priority, created_at"
    ).fetchall()
    tasks = [dict(r) for r in rows]
    for t in tasks:
        t["status"] = normalize_status(t["status"])
    # Add estimates
    est_rows = con.execute("SELECT * FROM task_estimates").fetchall()
    estimates = {r["task_id"]: dict(r) for r in est_rows}
    con.close()
    for t in tasks:
        e = estimates.get(t["id"], {})
        t["estimated_minutes"] = e.get("estimated_minutes", 0)
        t["buffer_percent"] = e.get("buffer_percent", 20)
    return tasks


def fetch_routines():
    con = get_db()
    rows = con.execute("SELECT * FROM routines ORDER BY category, name").fetchall()
    routines = [dict(r) for r in rows]
    # Add today's completion status and streak
    today_start = int(__import__("time").time()) - 86400  # last 24h
    for r in routines:
        log = con.execute(
            "SELECT COUNT(*) as cnt, MAX(completed_at) as last FROM routine_logs WHERE routine_id=? AND completed_at>?",
            (r["id"], today_start)
        ).fetchone()
        r["done_today"] = log["cnt"] > 0
        r["last_done"] = log["last"]
        # Streak: count consecutive days with logs
        streak = 0
        logs = con.execute(
            "SELECT completed_at FROM routine_logs WHERE routine_id=? ORDER BY completed_at DESC LIMIT 365",
            (r["id"],)
        ).fetchall()
        if logs:
            from datetime import datetime, timedelta
            # Just return last_completed for now
            pass
    con.close()
    return routines


def fetch_routine_log(routine_id):
    con = get_db()
    rows = con.execute(
        "SELECT * FROM routine_logs WHERE routine_id=? ORDER BY completed_at DESC LIMIT 30",
        (routine_id,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def fetch_ki_rules():
    con = get_db()
    rows = con.execute("SELECT keyword, project, priority, estimated_minutes FROM ki_rules ORDER BY corrected_at DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]

def fetch_brainstorms():
    con = get_db()
    rows = con.execute(
        "SELECT id, content, project, processed, created_at FROM brainstorm ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def ts_to_date(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return ""


def date_to_ts(date_str):
    if not date_str or not date_str.strip():
        return None
    try:
        return int(datetime.strptime(date_str.strip(), "%Y-%m-%d").timestamp())
    except ValueError:
        return None


class KanbanHandler(SimpleHTTPRequestHandler):

    # ── Passwortschutz ──
    def require_auth(self):
        if not AUTH_USER:
            return True  # Auth ist deaktiviert
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self._send_auth_challenge()
            return False
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            self._send_auth_challenge()
            return False
        if user == AUTH_USER and pwd == AUTH_PASS:
            return True
        self._send_auth_challenge()
        return False

    def _send_auth_challenge(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}"')
        self.end_headers()
        self.wfile.write(b"<h1>401 Unauthorized</h1><p>Zugriff nur mit g&uuml;ltigen Anmeldedaten.</p>")

    def _debug_sync(self):
        """Status des DB-Sync (ohne Auth, nur für Debugging)."""
        import json, subprocess, os
        status = {"token": bool(GITHUB_TOKEN), "git_dir": os.path.isdir(str(BASE / ".git"))}
        if status["git_dir"]:
            r = subprocess.run(["git", "remote", "-v"],
                               capture_output=True, text=True, cwd=str(BASE))
            status["remote_v"] = r.stdout.strip() or r.stderr.strip()
            r2 = subprocess.run(["git", "status", "--porcelain", str(KANBAN_DB.relative_to(BASE))],
                                capture_output=True, text=True, cwd=str(BASE))
            status["db_dirty"] = bool(r2.stdout.strip())
        status["log"] = SYNC_LOG[-20:]  # letzte 20 Log-Einträge
        self.send_json(status)

    def do_GET(self):
        # Debug-Endpoint ohne Auth (nur Sync-Status, keine Daten)
        if self.path.rstrip("/") == "/api/debug/sync":
            self._debug_sync()
            return
        if not self.require_auth():
            return
        path = self.path.rstrip("/")
        if path == "/":
            self.send_html(INDEX_HTML)
        elif path == "/api/tasks":
            self.send_json({"tasks": fetch_tasks()})
        elif path == "/api/routines":
            self.send_json({"routines": fetch_routines()})
        elif path.startswith("/api/routines/") and path.endswith("/log"):
            rid = path.split("/api/routines/")[1].split("/log")[0]
            self.send_json({"log": fetch_routine_log(rid)})
        elif path == "/api/ki-learn":
            keyword = body.get("keyword", "").strip().lower()
            if keyword:
                con = get_db()
                con.execute("INSERT OR REPLACE INTO ki_rules (keyword, project, priority, estimated_minutes, corrected_at) VALUES (?, ?, ?, ?, ?)",
                    (keyword,
                     body.get("project", ""),
                     body.get("priority", ""),
                     int(body.get("estimated_minutes", 0)),
                     int(__import__("time").time())))
                con.commit()
                con.close()
                sync_db_to_git()
                self.send_json({"status": "ok"})
            else:
                self.send_json({"error": "keyword required"}, 400)
        elif path == "/api/brainstorm":
            self.send_json({"entries": fetch_brainstorms()})
        elif path == "/api/ki-rules":
            self.send_json({"rules": fetch_ki_rules()})
        # ── Dashboard GET Endpoints ──
        elif path == "/api/habits":
            con = get_db()
            cur = con.execute("SELECT * FROM habits ORDER BY id")
            habits = [dict(r) for r in cur.fetchall()]
            today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            for h in habits:
                cur2 = con.execute("SELECT id FROM habit_logs WHERE habit_id=? AND log_date=?", (h["id"], today))
                h["done_today"] = cur2.fetchone() is not None
            con.close()
            self.send_json({"habits": habits})
        elif path == "/api/weekly-goals":
            ws = _week_start_ts()
            con = get_db()
            cur = con.execute("SELECT * FROM weekly_goals WHERE week_start=? ORDER BY id", (ws,))
            self.send_json({"goals": [dict(r) for r in cur.fetchall()]})
            con.close()
        elif path == "/api/revenue":
            ms = _month_start_ts()
            next_ms = int(__import__("datetime").datetime.fromtimestamp(ms).replace(month=__import__("datetime").datetime.fromtimestamp(ms).month+1 if __import__("datetime").datetime.fromtimestamp(ms).month<12 else 12).timestamp()) if False else ms + 30*86400
            conv_month = __import__("datetime").datetime.now().strftime("%Y-%m")
            con = get_db()
            cur = con.execute("SELECT source, SUM(amount) as total FROM revenue WHERE month=? GROUP BY source", (conv_month,))
            sources = cur.fetchall()
            cur2 = con.execute("SELECT SUM(amount) as total FROM revenue WHERE month=?", (conv_month,))
            total_row = cur2.fetchone()
            con.close()
            sources_dict = [dict(s) for s in sources]
            self.send_json({"total": total_row["total"] if total_row and total_row["total"] else 0, "by_source": sources_dict})
        elif path == "/api/important-emails":
            con = get_db()
            cur = con.execute("SELECT * FROM important_emails ORDER BY created_at DESC LIMIT 10")
            self.send_json({"emails": [dict(r) for r in cur.fetchall()]})
            con.close()
        elif path.startswith("/api/tasks/"):
            task_id = path.split("/api/tasks/")[1].split("/")[0]
            if task_id:
                con = get_db()
                row = con.execute(
                    "SELECT id, title, body, procedure, status, priority, created_at, started_at, "
                    "completed_at, assignee, project_ids FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                con.close()
                t = dict(row) if row else {"error": "not found"}
                if row:
                    con = get_db()
                    est = con.execute("SELECT * FROM task_estimates WHERE task_id=?", (task_id,)).fetchone()
                    con.close()
                    t["estimated_minutes"] = est["estimated_minutes"] if est else 0
                    t["buffer_percent"] = est["buffer_percent"] if est else 20
                self.send_json(t, 200 if row else 404)
            else:
                self.send_json({"error": "missing id"}, 400)
        else:
            self.send_html(INDEX_HTML)

    def do_POST(self):
        if not self.require_auth():
            return
        body = self._read_body()
        path = self.path.rstrip("/")

        if path == "/api/tasks":
            title = body.get("title", "Neue Aufgabe").strip() or "Neue Aufgabe"
            prio = PRIO_VALUES.get(body.get("priority", "mittel"), 2)
            desc = body.get("body", "").strip()
            status = body.get("status", "ready")
            if status not in STATUS_ORDER:
                status = "ready"
            start_ts = date_to_ts(body.get("start_date"))
            due_ts = date_to_ts(body.get("due_date"))
            est_min = int(body.get("estimated_minutes", 0))
            buf_pct = int(body.get("buffer_percent", 20))

            task_id = f"t_{uuid.uuid4().hex[:8]}"
            now = int(time.time())
            projects = body.get("projects", [])
            if isinstance(projects, str):
                projects = [p.strip() for p in projects.split(",") if p.strip()]
            project_ids_str = json.dumps(projects)
            proc = body.get("procedure", "").strip()
            con = get_db()
            con.execute(
                "INSERT INTO tasks (id, title, body, procedure, status, priority, created_at, assignee, started_at, completed_at, project_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, desc, proc, status, prio, now, "user", start_ts or None, due_ts or None, project_ids_str or None),
            )
            con.execute(
                "INSERT OR REPLACE INTO task_estimates (task_id, estimated_minutes, buffer_percent) VALUES (?, ?, ?)",
                (task_id, est_min, buf_pct),
            )
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True, "id": task_id})

        elif path == "/api/brainstorm":
            content = body.get("content", "").strip()
            projects = body.get("projects", [])
            if isinstance(projects, str):
                projects = [p.strip() for p in projects.split(",") if p.strip()]
            project_ids_str = json.dumps(projects)
            if not content:
                self.send_json({"error": "empty content"}, 400)
                return
            now = int(time.time())
            con = get_db()
            con.execute("INSERT INTO brainstorm (content, project, processed, created_at) VALUES (?, ?, 0, ?)",
                        (content, project, now))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})

        elif path.startswith("/api/routines/") and path.endswith("/toggle"):
            rid = int(path.split("/api/routines/")[1].split("/toggle")[0])
            now = int(time.time())
            con = get_db()
            log = con.execute(
                "SELECT COUNT(*) as cnt FROM routine_logs WHERE routine_id=? AND completed_at>?",
                (rid, now - 86400)
            ).fetchone()
            if log["cnt"] > 0:
                # Undo: remove today's entry
                con.execute("DELETE FROM routine_logs WHERE routine_id=? AND completed_at>?", (rid, now - 86400))
            else:
                con.execute("INSERT INTO routine_logs (routine_id, completed_at) VALUES (?, ?)", (rid, now))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})

        # ── Dashboard: POST habits toggle ──
        elif path.startswith("/api/habits/") and path.endswith("/toggle"):
            hid = path.split("/api/habits/")[1].split("/")[0]
            con = get_db()
            today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            cur = con.execute("SELECT id FROM habit_logs WHERE habit_id=? AND log_date=?", (hid, today))
            existing = cur.fetchone()
            if existing:
                con.execute("DELETE FROM habit_logs WHERE id=?", (existing["id"],))
            else:
                con.execute("INSERT INTO habit_logs (habit_id, log_date) VALUES (?, ?)", (hid, today))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})

        # ── Dashboard: POST habits create ──
        elif path == "/api/habits":
            if not body or not body.get("name"):
                self.send_json({"error": "name required"}, 400)
                return
            con = get_db()
            con.execute("INSERT INTO habits (name, icon) VALUES (?, ?)", (body["name"], body.get("icon", "✅")))
            con.commit()
            new_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True, "id": new_id})

        # ── Dashboard: POST weekly goals create ──
        elif path == "/api/weekly-goals":
            if not body or not body.get("title"):
                self.send_json({"error": "title required"}, 400)
                return
            ws = _week_start_ts()
            con = get_db()
            con.execute("INSERT INTO weekly_goals (title, icon, target_value, current_value, unit, week_start) VALUES (?,?,?,?,?,?)",
                        (body["title"], body.get("icon","🎯"), body.get("target_value") if body.get("target_value") is not None else 1, body.get("current_value") if body.get("current_value") is not None else 0, body.get("unit","x"), ws))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        # ── Dashboard: POST goal complete (increment) ──
        elif path == "/api/weekly-goals/complete":
            if not body or body.get("id") is None:
                self.send_json({"error": "id required"}, 400)
                return
            con = get_db()
            con.execute("UPDATE weekly_goals SET current_value = MIN(current_value + 1, target_value) WHERE id=?", (body["id"],))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        # ── Dashboard: POST revenue create ──
        elif path == "/api/revenue":
            if not body or not body.get("source") or body.get("amount") is None:
                self.send_json({"error": "source and amount required"}, 400)
                return
            conv_month = __import__("datetime").datetime.now().strftime("%Y-%m")
            con = get_db()
            con.execute("INSERT INTO revenue (source, amount, month) VALUES (?,?,?)",
                        (body["source"], float(body["amount"]), conv_month))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        # ── Dashboard: POST important email create ──
        elif path == "/api/important-emails":
            if not body or not body.get("sender") or not body.get("subject"):
                self.send_json({"error": "sender and subject required"}, 400)
                return
            con = get_db()
            con.execute("INSERT INTO important_emails (sender, subject, preview, priority) VALUES (?,?,?,?)",
                        (body["sender"], body["subject"], body.get("preview",""), body.get("priority","mittel")))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})

        elif path.startswith("/api/tasks/") and path.endswith("/status"):
            task_id = path.split("/api/tasks/")[1].split("/status")[0]
            new_status = body.get("status", "")
            if new_status not in STATUS_ORDER:
                self.send_json({"error": f"invalid status: {new_status}"}, 400)
                return
            now = int(time.time())
            con = get_db()
            if new_status == "running":
                con.execute("UPDATE tasks SET status=?, started_at=COALESCE(started_at,?) WHERE id=?", (new_status, now, task_id))
            elif new_status == "completed":
                con.execute("UPDATE tasks SET status=?, completed_at=COALESCE(completed_at,?) WHERE id=?", (new_status, now, task_id))
            else:
                con.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, task_id))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        body = self._read_body()
        path = self.path.rstrip("/")

        if path.startswith("/api/tasks/"):
            task_id = path.split("/api/tasks/")[1].split("/")[0]
            if not task_id:
                self.send_json({"error": "missing id"}, 400)
                return

            updates = []
            params = []

            title = body.get("title", "").strip()
            desc = body.get("body", "").strip()
            prio_str = body.get("priority", "")
            start_str = body.get("start_date", "")
            due_str = body.get("due_date", "")
            projects = body.get("projects", [])
            if isinstance(projects, str):
                projects = [p.strip() for p in projects.split(",") if p.strip()]
            project_ids_str = json.dumps(projects) if projects else "[]"
            est_min = body.get("estimated_minutes")
            buf_pct = body.get("buffer_percent")

            if title:
                updates.append("title=?")
                params.append(title)
            if body.get("body") is not None:
                updates.append("body=?")
                params.append(desc)
            if prio_str:
                prio = PRIO_VALUES.get(prio_str)
                if prio:
                    updates.append("priority=?")
                    params.append(prio)
            if body.get("start_date") is not None:
                start_ts = date_to_ts(start_str)
                updates.append("started_at=?")
                params.append(start_ts)
            if body.get("due_date") is not None:
                due_ts = date_to_ts(due_str)
                updates.append("completed_at=?")
                params.append(due_ts)
            if body.get("projects") is not None:
                updates.append("project_ids=?")
                params.append(project_ids_str)
            if body.get("procedure") is not None:
                updates.append("procedure=?")
                params.append(body.get("procedure", "").strip())

            if updates:
                params.append(task_id)
                con = get_db()
                con.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)
                con.commit()
                con.close()

            # Update estimates
            if est_min is not None or buf_pct is not None:
                con = get_db()
                current = con.execute("SELECT * FROM task_estimates WHERE task_id=?", (task_id,)).fetchone()
                if current:
                    e_min = est_min if est_min is not None else current["estimated_minutes"]
                    b_pct = buf_pct if buf_pct is not None else current["buffer_percent"]
                    con.execute("UPDATE task_estimates SET estimated_minutes=?, buffer_percent=? WHERE task_id=?",
                                (e_min, b_pct, task_id))
                else:
                    con.execute("INSERT INTO task_estimates (task_id, estimated_minutes, buffer_percent) VALUES (?, ?, ?)",
                                (task_id, est_min or 0, buf_pct or 20))
                con.commit()
                con.close()

            sync_db_to_git()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.require_auth():
            return
        path = self.path.rstrip("/")
        if path.startswith("/api/tasks/"):
            task_id = path.split("/api/tasks/")[1].split("/")[0]
            if not task_id:
                self.send_json({"error": "missing id"}, 400)
                return
            con = get_db()
            con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            con.execute("DELETE FROM task_estimates WHERE task_id=?", (task_id,))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        elif path.startswith("/api/brainstorm/"):
            entry_id = path.split("/api/brainstorm/")[1].split("/")[0]
            con = get_db()
            con.execute("DELETE FROM brainstorm WHERE id=?", (entry_id,))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        # ── Dashboard DELETE endpoints ──
        elif path.startswith("/api/habits/"):
            hid = path.split("/api/habits/")[1].split("/")[0]
            con = get_db()
            con.execute("DELETE FROM habits WHERE id=?", (hid,))
            con.execute("DELETE FROM habit_logs WHERE habit_id=?", (hid,))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        elif path.startswith("/api/revenue/"):
            rid = path.split("/api/revenue/")[1].split("/")[0]
            con = get_db()
            con.execute("DELETE FROM revenue WHERE id=?", (rid,))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        elif path.startswith("/api/important-emails/"):
            eid = path.split("/api/important-emails/")[1].split("/")[0]
            con = get_db()
            con.execute("DELETE FROM important_emails WHERE id=?", (eid,))
            con.commit()
            con.close()
            sync_db_to_git()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass


# Das <title> und die h1-Überschrift anpassen
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LiveOS · Paul Dubrownik</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0e17;--surface:#111827;--surf2:#1a2332;--surf3:#1e293b;--text:#e2e8f0;--dim:#94a3b8;--faint:#475569;--accent:#00ff88;--accent2:#00cc6a;--accent-dim:#0a3a1e;--glow:rgba(0,255,136,.15);--border:#1e293b;--border2:#2d3a4e;--danger:#ff3355;--warn:#f59e0b;--ph:#ff3355;--pm:#f59e0b;--pl:#6b7280}
body{font-family:Inter,system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
::-webkit-scrollbar{width:6px;height:6px;background:var(--surface)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.header{background:linear-gradient(135deg,var(--surface),var(--surf2));border-bottom:1px solid var(--border);padding:20px 28px 16px;position:sticky;top:0;z-index:100}
.h-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.greet{font-size:24px;font-weight:700}.greet .t{font-weight:400;color:var(--dim);font-size:16px;margin-left:8px}
.greet .a{color:var(--accent)}
.h-stats{display:flex;gap:20px;font-size:13px;color:var(--dim)}
.h-stats span{display:flex;align-items:center;gap:4px}
.h-stats .n{color:var(--accent);font-weight:600;font-size:15px}
.smart-w{display:flex;gap:8px;align-items:center}
.smart-i{flex:1;background:var(--surf3);border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-size:14px;color:var(--text);font-family:inherit;outline:none;transition:.2s}
.smart-i:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
.smart-i::placeholder{color:var(--faint)}
.smart-b{background:var(--accent);color:#0a0e17;border:none;border-radius:10px;padding:12px 20px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;transition:.2s;white-space:nowrap}
.smart-b:hover{background:var(--accent2);transform:translateY(-1px)}
.tab-bar{display:flex;gap:0;background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;position:sticky;top:115px;z-index:99}
.tab{display:flex;align-items:center;gap:6px;padding:12px 20px;cursor:pointer;font-size:13px;font-weight:500;color:var(--dim);background:transparent;border:none;border-bottom:2px solid transparent;transition:.2s;font-family:inherit}
.tab:hover{color:var(--text);background:var(--glow)}.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.cont{padding:20px 28px}
.panel{display:none}.panel.active{display:block}
.dash{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;transition:.2s}
.card:hover{border-color:var(--border2);box-shadow:0 6px 28px var(--glow)}
.card .phdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border2)}
.card .pt{font-size:13px;font-weight:600;color:var(--dim);display:flex;align-items:center;gap:6px;text-transform:uppercase;letter-spacing:.5px}
.card .pc{background:var(--accent-dim);color:var(--accent);font-size:10px;border-radius:10px;padding:1px 8px;font-weight:600}
.ti{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;transition:.2s;margin-bottom:4px;background:var(--surf2)}
.ti:hover{background:var(--surf3)}
.ti .tt{font-size:13px;font-weight:500;line-height:1.3;flex:1}
.ti .tm{font-size:10px;color:var(--faint);margin-top:2px;display:flex;gap:6px}
.ti .chk{width:20px;height:20px;border-radius:50%;border:2px solid var(--border2);cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.2s;font-size:10px}
.ti .chk:hover{border-color:var(--accent);background:var(--glow)}
.ti .chk.done{background:var(--accent);border-color:var(--accent);color:#0a0e17}
.cal-m{font-size:12px}
.cal-m .cd{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-top:6px}
.cal-m .cd div{padding:4px 0;text-align:center;border-radius:4px;font-size:11px;color:var(--dim)}
.cal-m .cd .td{background:var(--accent);color:#0a0e17;font-weight:600}
.cal-m .cd .ht{color:var(--accent);font-weight:600}
.cal-m .ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.cal-m .ch span{font-weight:600;color:var(--text)}
.cal-m .cn{background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;padding:2px 6px;border-radius:4px;transition:.2s}
.cal-m .cn:hover{color:var(--accent);background:var(--glow)}
.cal-m .cw{font-size:9px;color:var(--faint);text-align:center;padding:2px 0;font-weight:500;text-transform:uppercase}
.hi{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;margin-bottom:4px;background:var(--surf2);cursor:pointer;transition:.2s}
.hi:hover{background:var(--surf3)}
.hi .hi-icon{font-size:16px}.hi .hi-n{font-size:13px;flex:1}
.hi .hi-d{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;background:var(--surf3);color:var(--faint);flex-shrink:0;transition:.2s}
.hi .hi-d.done{background:var(--accent);color:#0a0e17}
.ha{display:flex;gap:6px;margin-top:8px}
.ha input{flex:1;background:var(--surf3);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:12px;color:var(--text);font-family:inherit;outline:none}
.ha input:focus{border-color:var(--accent)}
.ha button{background:var(--accent);color:#0a0e17;border:none;border-radius:6px;padding:8px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}
.ha button:hover{background:var(--accent2)}
.go{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.go .gi{font-size:14px}.go .gif{flex:1}
.go .gt{font-size:12px;font-weight:500}
.go .gb{height:4px;background:var(--surf3);border-radius:3px;margin-top:4px;overflow:hidden}
.go .gf{height:100%;background:var(--accent);border-radius:3px;transition:width .5s}
.go .gl{font-size:10px;color:var(--faint);margin-top:2px}
.rt{font-size:28px;font-weight:700;color:var(--accent);margin-bottom:4px}
.rl{font-size:11px;color:var(--faint);text-transform:uppercase;margin-bottom:12px}
.rs{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
.rs .rn{color:var(--dim)}.rs .ra{color:var(--accent);font-weight:600}
.em{display:flex;gap:10px;padding:10px 12px;border-radius:8px;margin-bottom:4px;background:var(--surf2);cursor:pointer;transition:.2s}
.em:hover{background:var(--surf3)}
.em .es{font-size:12px;font-weight:600;margin-bottom:2px}
.em .ej{font-size:11px;color:var(--dim)}
.em .ep{font-size:10px;color:var(--faint);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.em .epr{width:4px;border-radius:2px;flex-shrink:0;align-self:stretch}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:500;transition:.2s;font-family:inherit}
.btn-p{background:var(--accent);color:#0a0e17}.btn-p:hover{background:var(--accent2);transform:translateY(-1px)}
.btn-g{background:transparent;color:var(--dim);border:1px solid var(--border);padding:7px 15px}
.btn-g:hover{background:var(--glow);color:var(--text)}
.btn-s{padding:4px 10px;font-size:11px}
.kan{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;min-height:50vh}
.col{background:var(--surf2);border-radius:12px;padding:12px;min-height:200px;display:flex;flex-direction:column;border:1px solid var(--border)}
.colh{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border2);font-size:12px;color:var(--dim)}
.colh h3{font-size:12px;font-weight:600}
.colc{background:var(--accent-dim);color:var(--accent);font-size:10px;border-radius:10px;padding:1px 7px;font-weight:600}
.colb{flex:1;min-height:100px}
.ka{background:var(--surface);border-radius:10px;padding:12px;margin-bottom:6px;cursor:pointer;border-left:3px solid var(--faint);position:relative;transition:.2s}
.ka:hover{box-shadow:0 2px 10px rgba(0,0,0,.3)}
.ka.ph{border-left-color:var(--ph)}.ka.pm{border-left-color:var(--pm)}.ka.pl{border-left-color:var(--pl)}
.ka.dr{opacity:.4}.colb.do{background:var(--glow);border-radius:8px;min-height:60px}
.kt{font-size:13px;font-weight:600;margin-bottom:2px;padding-right:40px}
.kb{font-size:11px;color:var(--dim);line-height:1.3;margin-top:4px}
.km{font-size:10px;color:var(--faint);margin-top:6px;display:flex;gap:8px;flex-wrap:wrap}
.ka .kaa{position:absolute;top:6px;right:6px;display:flex;gap:2px}
.ka .kaa button{background:none;border:none;cursor:pointer;font-size:14px;color:var(--faint);padding:2px 4px;border-radius:4px}
.ka .kaa button:hover{background:var(--glow);color:var(--accent)}
.ss{font-size:10px;padding:3px 6px;border:1px solid var(--border);border-radius:5px;background:var(--surface);cursor:pointer;color:var(--dim);font-family:inherit}
.gan{overflow-x:auto;background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border)}
.gt{width:100%;border-collapse:collapse;font-size:13px;min-width:750px}
.gt th{background:var(--surf2);color:var(--dim);padding:8px 12px;text-align:left;font-size:12px;position:sticky;top:0;border-bottom:1px solid var(--border)}
.gt td{padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:middle}
.modal-o{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.8);z-index:1000;justify-content:center;align-items:center;padding:20px;backdrop-filter:blur(4px)}
.modal-o.show{display:flex}
.modal{background:var(--surface);border-radius:14px;padding:28px;max-width:600px;width:100%;max-height:85vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5);border:1px solid var(--border);animation:fi .2s}
@keyframes fi{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.mc{float:right;font-size:22px;cursor:pointer;color:var(--dim);line-height:1}.mc:hover{color:var(--accent)}
.modal h2{color:var(--accent);font-size:18px;margin-bottom:16px}
.modal lab{display:block;font-size:12px;font-weight:500;color:var(--dim);margin-bottom:4px;margin-top:12px}
.modal input,.modal textarea,.modal select{width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;font-family:inherit;background:var(--surf3);color:var(--text);outline:none;transition:.2s}
.modal input:focus,.modal textarea:focus,.modal select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
.modal textarea{min-height:80px;resize:vertical}
.modal .r2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.modal .br{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
.pf{position:fixed;bottom:24px;right:24px;width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#0a0e17;border:none;cursor:pointer;font-size:22px;box-shadow:0 4px 16px var(--glow);z-index:900;transition:.2s}
.pf:hover{transform:scale(1.1)}
.po{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.9);z-index:9998;justify-content:center;align-items:center}
.po.show{display:flex}
.pc{background:var(--surface);border-radius:20px;padding:40px;text-align:center;max-width:360px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.5);border:1px solid var(--border);position:relative}
.pc .pp{font-size:14px;font-weight:600;color:var(--accent);margin-bottom:8px}
.pc .ptm{font-size:72px;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;margin:16px 0}
.pc .cp{position:absolute;top:12px;right:12px;font-size:24px;cursor:pointer;color:var(--dim);background:none;border:none}
.pc .cp:hover{color:var(--accent)}
.em2{padding:30px 10px;text-align:center;color:var(--faint);font-size:13px}
.load{text-align:center;padding:40px;color:var(--dim);font-size:14px}
.sp{display:inline-block;width:20px;height:20px;border:2px solid var(--border2);border-top-color:var(--accent);border-radius:50%;animation:sp .6s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}
.tb{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
@media(max-width:900px){.header{padding:16px}.h-top{flex-direction:column;gap:8px}.cont{padding:16px}.dash{grid-template-columns:1fr}.tab-bar{padding:0 16px;overflow-x:auto}.kan{grid-template-columns:repeat(2,1fr)}.smart-w{flex-direction:column}}
@media(max-width:600px){.kan{grid-template-columns:1fr}.greet{font-size:20px}.tab{font-size:12px;padding:10px 12px}}
</style>
</head>
<body>

<div class="header">
  <div class="h-top">
    <div>
      <div class="greet">
        <span id="grtxt">Guten Morgen</span>, <span class="a">Paulo</span>
        <span class="t" id="clk"></span>
      </div>
    </div>
    <div class="h-stats" id="hstats">
      <span>Aufgaben: <span class="n" id="stT">0</span></span>
      <span>Erledigt: <span class="n" id="stD">0</span></span>
      <span>Ziele: <span class="n" id="stG">0</span></span>
    </div>
  </div>
  <div class="smart-w">
    <input class="smart-i" id="si" placeholder="Neue Aufgabe, Ziel oder Idee ..." onkeydown="if(event.key==='Enter')hSI()">
    <button class="smart-b" onclick="hSI()">Senden</button>
  </div>
</div>

<div class="tab-bar">
  <button class="tab active" onclick="swT('dash')">Dashboard</button>
  <button class="tab" onclick="swT('kan')">Kanban</button>
  <button class="tab" onclick="swT('cal')">Kalender</button>
  <button class="tab" onclick="swT('gan')">Gantt</button>
  <button class="tab" onclick="swT('set')">Einstellungen</button>
</div>

<div class="cont">

<div id="p-dash" class="panel active">
  <div class="dash">
    <div class="card"><div class="phdr"><span class="pt">Hochprior</span><span class="pc" id="cH">0</span></div><div id="tH"><div class="em2">Keine</div></div></div>
    <div class="card"><div class="phdr"><span class="pt">Mittlere</span><span class="pc" id="cM">0</span></div><div id="tM"><div class="em2">Keine</div></div></div>
    <div class="card"><div class="phdr"><span class="pt">Niedrige</span><span class="pc" id="cL">0</span></div><div id="tL"><div class="em2">Keine</div></div></div>
    <div class="card"><div class="phdr"><span class="pt">Kalender</span></div><div class="cal-m" id="calM"></div></div>
    <div class="card"><div class="phdr"><span class="pt">Habits</span><span class="pc" id="cHb">0</span></div><div id="hList"></div><div class="ha"><input id="nHi" placeholder="Neues Habit ..." onkeydown="if(event.key==='Enter')aHb()"><button onclick="aHb()">+</button></div></div>
    <div class="card"><div class="phdr"><span class="pt">Wochenziele</span></div><div id="gList"><div class="em2">Keine</div></div></div>
    <div class="card"><div class="phdr"><span class="pt">Einnahmen</span></div><div class="rt" id="revT">0,00</div><div class="rl">Diesen Monat</div><div id="revS"></div><div class="ha"><input id="revSi" placeholder="Quelle ..." style="width:35%"><input id="revAi" type="number" step="0.01" min="0" placeholder="Betrag ..." style="width:30%"><button onclick="aRev()">+</button></div></div>
    <div class="card"><div class="phdr"><span class="pt">Wichtige Mails</span><span class="pc" id="cEm">0</span></div><div id="eList"><div class="em2">Keine</div></div></div>
  </div>
</div>

<div id="p-kan" class="panel">
  <div class="tb">
    <button class="btn btn-p" onclick="oCM()">+ Neue Karte</button>
    <select id="pFil" onchange="rKan()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;background:var(--surf3);color:var(--text)"><option value="">Alle Projekte</option></select>
    <button class="btn btn-g" onclick="lT()">Aktualisieren</button>
  </div>
  <div id="kanA" class="kan"><div class="load"><span class="sp"></span>Lade ...</div></div>
</div>

<div id="p-cal" class="panel">
  <div class="tb">
    <button class="btn btn-g" onclick="cN(-1)">&lt;</button>
    <span id="cLab" style="font-size:14px;font-weight:600;color:var(--accent);min-width:240px;text-align:center"></span>
    <button class="btn btn-g" onclick="cN(1)">&gt;</button>
    <button class="btn btn-g" onclick="cT()">Heute</button>
    <select id="cFil" onchange="rCal()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;background:var(--surf3);color:var(--text)"><option value="">Alle Projekte</option></select>
  </div>
  <div id="calW" style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border)"><div class="load"><span class="sp"></span>Lade ...</div></div>
</div>

<div id="p-gan" class="panel">
  <div class="tb">
    <select id="gFil" onchange="rGan()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;background:var(--surf3);color:var(--text)"><option value="">Alle Projekte</option></select>
    <button class="btn btn-g" onclick="lT()">Aktualisieren</button>
  </div>
  <div id="ganW" class="gan"><div class="load"><span class="sp"></span>Lade ...</div></div>
</div>

<div id="p-set" class="panel">
  <div style="max-width:500px;margin:0 auto">
    <div class="card" style="margin-bottom:16px">
      <div class="phdr"><span class="pt">Einstellungen</span></div>
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer"><span>Dark Mode</span><input type="checkbox" id="thT" checked onchange="tTh()" style="accent-color:var(--accent)"></label>
    </div>
  </div>
</div>

</div>

<!-- Modal -->
<div class="modal-o" id="mO" onclick="if(event.target===this)cMo()">
  <div class="modal"><span class="mc" onclick="cMo()">&times;</span><h2 id="mT">Aufgabe</h2><div id="mB"></div></div>
</div>

<!-- Pomodoro -->
<button class="pf" id="pf" onclick="tPo()">&#x1f345;</button>
<div class="po" id="pO">
  <div class="pc">
    <button class="cp" onclick="tPo()">&times;</button>
    <div class="pp" id="pPh">&#x1f345; Fokus</div>
    <div class="ptm" id="pTi">25:00</div>
    <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
      <button class="btn btn-p" id="pBtn" onclick="pSt()">&#x25b6; Start</button>
      <button class="btn btn-g" onclick="pRe()">Reset</button>
    </div>
    <div class="pp" style="margin-top:16px;font-size:12px;color:var(--dim)" id="pCo">&#x1f345; 0 Today</div>
  </div>
</div>

// State
let T=[], H=[], G=[], R=[];
let pS='idle', pR=25*60, pI=null, pC=parseInt(localStorage.getItem('pC')||'0'), pD=localStorage.getItem('pD')||'';
const td=new Date().toDateString();
if(pD!==td){pC=0;localStorage.setItem('pD',td)}
localStorage.setItem('pC',pC);

// API
async function ap(p,o={}){return fetch(p,{headers:{'Content-Type':'application/json',...o.headers},...o}).then(r=>r.json())}

// Clock
function uCl(){
  const n=new Date(), h=n.getHours().toString().padStart(2,'0'), m=n.getMinutes().toString().padStart(2,'0');
  document.getElementById('clk').textContent=h+':'+m;
  const gr=n.getHours();
  document.getElementById('grtxt').textContent=gr<12?'Guten Morgen':gr<17?'Guten Tag':gr<22?'Guten Abend':'Gute Nacht';
}
uCl();setInterval(uCl,30000);

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function pL(p){if(typeof p==='number')return {1:'hoch',2:'mittel',3:'niedrig'}[p]||'mittel';if(['hoch','mittel','niedrig'].includes(p))return p;return 'mittel'}
function gP(t){try{const p=JSON.parse(t.project_ids||'[]');return Array.isArray(p)?p:[]}catch(e){return []}}
function fM(m){if(!m||m<=0)return '';if(m>=60)return Math.floor(m/60)+'h '+(m%60?m%60+'min':'');return m+'min'}
const Co=['backlog','ready','running','completed','blocked'];
const Cn={'backlog':'Backlog','ready':'Bereit','running':'In Arbeit','completed':'Erledigt','blocked':'Blockiert'};
const Cl={'hoch':'#ff3355','mittel':'#f59e0b','niedrig':'#6b7280'};

// Load all data
async function lT(){
  try{
    const [td,hd,gd]=await Promise.all([ap('/api/tasks'),ap('/api/habits'),ap('/api/weekly-goals')]);
    T=td.tasks||[];H=hd.habits||[];G=gd.goals||[];
    document.getElementById('stT').textContent=T.length;
    document.getElementById('stD').textContent=T.filter(t=>t.status==='completed').length;
    document.getElementById('stG').textContent=G.length;
    rDa();rKan();rGan();rCal();
    // Filters
    const pj=[...new Set(T.flatMap(t=>gP(t)).filter(Boolean))].sort();
    const op=pj.map(p=>'<option value="'+esc(p)+'">'+esc(p)+'</option>').join('');
    ['pFil','gFil','cFil'].forEach(id=>{const s=document.getElementById(id);if(s){const v=s.value;s.innerHTML='<option value="">Alle Projekte</option>'+op;s.value=v}});
  }catch(e){console.error(e)}
}

// Dashboard
function rDa(){
  rTo();rCM();rHb();rGo();rRe();rEm();
}
function rTo(){
  const a=T.filter(t=>t.status!=='completed');
  ['hoch','mittel','niedrig'].forEach(prio=>{
    const items=a.filter(t=>pL(t.priority)===prio).slice(0,5);
    const cid={hoch:'tH',mittel:'tM',niedrig:'tL'}[prio];
    const cc={hoch:'cH',mittel:'cM',niedrig:'cL'}[prio];
    const el=document.getElementById(cid);const ce=document.getElementById(cc);
    if(ce)ce.textContent=items.length;
    if(!items.length){el.innerHTML='<div class="em2">Keine</div>';return}
    el.innerHTML=items.map(t=>{
      const pj=gP(t);const ps=pj.length?' | '+pj.join(', '):'';
      return '<div class="ti" onclick="eT('+"'"+t.id+"'"+')">'
        +'<div class="chk" onclick="event.stopPropagation();qC('+"'"+t.id+"'"+')">&#x2713;</div>'
        +'<div style="flex:1"><div class="tt">'+esc(t.title)+'</div>'
        +'<div class="tm">'+ps+(t.estimated_minutes?' | '+fM(t.estimated_minutes):'')+'</div></div></div>';
    }).join('');
  });
}
async function qC(id){await ap('/api/tasks/'+id+'/status',{method:'POST',body:JSON.stringify({status:'completed'})});await lT()}

// Mini Calendar
function rCM(){
  const n=new Date(), y=n.getFullYear(), m=n.getMonth(), f=new Date(y,m,1), l=new Date(y,m+1,0);
  const sd=(f.getDay()+6)%7, di=l.getDate();
  const ms=Math.floor(f.getTime()/1000), me=ms+di*86400;
  const td_=new Set();T.forEach(t=>{const s=t.started_at||t.created_at;if(s&&s>=ms&&s<me){const d=new Date(s*1000);td_.add(d.getDate())}});
  const mn=f.toLocaleDateString('de-DE',{month:'long',year:'numeric'});
  const wd=['Mo','Di','Mi','Do','Fr','Sa','So'];
  const tdN=n.getDate();
  let h='<div class="ch"><button class="cn" onclick="cMN(-1)">&lt;</button><span>'+mn+'</span><button class="cn" onclick="cMN(1)">&gt;</button></div><div class="cd">';
  wd.forEach(d=>{h+='<div class="cw">'+d+'</div>'});
  for(let i=0;i<sd;i++)h+='<div></div>';
  for(let d=1;d<=di;d++){const cls=d===tdN?'td':td_.has(d)?'ht':'';h+='<div class="'+cls+'">'+d+'</div>'}
  h+='</div>';
  document.getElementById('calM').innerHTML=h;
}
let cMM=0;
function cMN(d){cMM+=d;rCM()}

// Habits
function rHb(){
  const el=document.getElementById('hList');const ce=document.getElementById('cHb');
  if(ce)ce.textContent=H.length;
  if(!H.length){el.innerHTML='<div class="em2">Keine Habits</div>';return}
  el.innerHTML=H.map(h=>'<div class="hi" onclick="tHb('+h.id+')"><span class="hi-icon">'+(h.icon||'&#x2705;')+'</span><span class="hi-n">'+esc(h.name)+'</span><div class="hi-d '+(h.done_today?'done':'')+'">'+(h.done_today?'&#x2713;':'&#x25CB;')+'</div></div>').join('');
}
async function tHb(id){await ap('/api/habits/'+id+'/toggle',{method:'POST'});const hd=await ap('/api/habits');H=hd.habits||[];rHb()}
async function aHb(){const i=document.getElementById('nHi');const n=i.value.trim();if(!n)return;await ap('/api/habits',{method:'POST',body:JSON.stringify({name:n,icon:'&#x2705;'})});i.value='';const hd=await ap('/api/habits');H=hd.habits||[];rHb()}

// Goals
function rGo(){
  const el=document.getElementById('gList');
  if(!G.length){el.innerHTML='<div class="em2">Keine</div>';return}
  el.innerHTML=G.map(g=>{
    const pct=g.target_value>0?Math.min(100,Math.round(g.current_value/g.target_value*100)):0;
    return '<div class="go"><span class="gi">'+(g.icon||'&#x1f3af;')+'</span><div class="gif"><div class="gt">'+esc(g.title)+'</div><div class="gb"><div class="gf" style="width:'+pct+'%"></div></div><div class="gl">'+g.current_value+'/'+g.target_value+' '+g.unit+' ('+pct+'%)</div></div></div>';
  }).join('');
}

// Revenue
async function rRe(){
  try{const rd=await ap('/api/revenue');document.getElementById('revT').innerHTML=Number(rd.total).toLocaleString('de-DE',{minimumFractionDigits:2})+' &euro;';
  const ss=rd.by_source||[];const co=document.getElementById('revS');
  if(!ss.length){co.innerHTML='<div class="em2">Keine</div>';return}
  co.innerHTML=ss.map(s=>'<div class="rs"><span class="rn">'+esc(s.source)+'</span><span class="ra">'+Number(s.total).toFixed(2).replace('.',',')+' &euro;</span></div>').join('')
  }catch(e){}}
async function rEm(){
  try{const ed=await ap('/api/important-emails');const em=ed.emails||[];document.getElementById('cEm').textContent=em.length;
  const co=document.getElementById('eList');if(!em.length){co.innerHTML='<div class="em2">Keine</div>';return}
  co.innerHTML=em.map(e=>'<div class="em" onclick="mER('+e.id+')"><div class="epr" style="background:'+(e.priority==='hoch'?'var(--danger)':e.priority==='mittel'?'var(--warn)':'var(--pl)')+'"></div><div style="flex:1;opacity:'+(e.is_read?'.6':'1')+'"><div class="es">'+esc(e.sender)+'</div><div class="ej">'+esc(e.subject)+'</div><div class="ep">'+esc(e.preview)+'</div></div></div>').join('')
  }catch(e){}}
async function mER(id){await ap('/api/important-emails/'+id,{method:'DELETE'});rEm()}

// Smart Input
async function hSI(){
  const i=document.getElementById('si');const t=i.value.trim();if(!t)return;i.value='';i.placeholder='Verarbeite ...';
  try{
    let prio='mittel',proj=[],est=0;
    if(/^!!|dringend|sofort|wichtig/i.test(t))prio='hoch';else if(/irgendwann|vielleicht|optional/i.test(t))prio='niedrig';
    const dm=t.match(/(\d+)\s*(min|minuten|h|stunden)/i);
    if(dm)est=dm[2].toLowerCase().startsWith('h')||dm[2].startsWith('st')?parseInt(dm[1])*60:parseInt(dm[1]);
    const pm=t.match(/#(\w+)/g);if(pm)proj=pm.map(p=>p.replace('#',''));
    const title=t.replace(/^!!\s*/,'').replace(/#\w+/g,'').replace(/\d+\s*(min|minuten|h|stunden)/gi,'').replace(/\(\s*\d+\s*(min|minuten|h|stunden)\s*\)/gi,'').trim();
    await ap('/api/tasks',{method:'POST',body:JSON.stringify({title:title||t,priority:prio,projects:proj,estimated_minutes:est,status:prio==='hoch'?'ready':'backlog'})});
    i.placeholder='Erstellt!';await lT();
  }catch(e){i.placeholder='Fehler: '+e.message}
  setTimeout(()=>{i.placeholder='Neue Aufgabe, Ziel oder Idee ...'},2000);
}

// Kanban
function rKan(){
  const f=document.getElementById('pFil').value;
  const filtered=f?T.filter(t=>gP(t).includes(f)):T;
  const ka=document.getElementById('kanA');
  ka.innerHTML=Co.map(col=>{
    const items=filtered.filter(t=>{const s=t.status||'backlog';return Co.includes(s)?s===col:false});
    return '<div class="col"><div class="colh"><h3>'+Cn[col]+'</h3><span class="colc">'+items.length+'</span></div>'
      +'<div class="colb" data-c="'+col+'" ondragover="oDO(event)" ondrop="oDr(event)" ondragleave="oDL(event)">'
      +(!items.length?'<div class="em2">--</div>':items.map(t=>rCa(t)).join(''))+'</div></div>';
  }).join('');
}
function rCa(t){
  const p=pL(t.priority);const bs=t.body?t.body.substring(0,80)+(t.body.length>80?'...':''):'';
  const pj=gP(t);const ph=pj.map(p=>'<span style="background:var(--surf3);border-radius:3px;padding:1px 5px;font-size:9px;color:var(--dim);margin:0 1px">'+esc(p)+'</span>').join('');
  return '<div class="ka '+p+'" data-id="'+t.id+'" draggable="true" ondragstart="oDS(event)" onclick="eT('+"'"+t.id+"'"+')">'
    +'<div class="kaa" onclick="event.stopPropagation()"><button onclick="event.stopPropagation();dT('+"'"+t.id+"'"+')">&#x1f5d1;</button></div>'
    +'<div class="kt">'+esc(t.title)+'</div>'+(bs?'<div class="kb">'+esc(bs)+'</div>':'')
    +'<div class="km"><span style="color:'+(Cl[p]||'#999')+';font-weight:600">'+p+'</span>'+ph
    +'<select class="ss" onclick="event.stopPropagation()" onchange="cS('+"'"+t.id+"'"+',this.value)">'
    +Co.map(s=>'<option value="'+s+'" '+(s===t.status?'selected':'')+'>'+Cn[s]+'</option>').join('')
    +'</select></div></div>';
}
async function cS(id,ns){if(!Co.includes(ns))return;try{await ap('/api/tasks/'+id+'/status',{method:'POST',body:JSON.stringify({status:ns})})}finally{await lT()}}
async function dT(id){if(!confirm('L&ouml;schen?'))return;await ap('/api/tasks/'+id,{method:'DELETE'});await lT()}

function oDS(e){e.dataTransfer.setData('text/plain',e.currentTarget.dataset.id);e.dataTransfer.effectAllowed='move';e.currentTarget.classList.add('dr')}
function oDO(e){e.preventDefault();e.dataTransfer.dropEffect='move';e.currentTarget.classList.add('do')}
function oDL(e){e.currentTarget.classList.remove('do')}
function oDr(e){e.preventDefault();e.currentTarget.classList.remove('do');const tid=e.dataTransfer.getData('text/plain');const col=e.currentTarget.dataset.c;if(tid&&col)cS(tid,col)}
document.addEventListener('dragend',e=>{e.target.classList.remove('dr');document.querySelectorAll('.do').forEach(el=>el.classList.remove('do'))});

// Modal
function oCM(){
  document.getElementById('mT').textContent='Neue Aufgabe';
  document.getElementById('mB').innerHTML='<lab>Titel *</lab><input type="text" id="fT" placeholder="Aufgabe ..."><lab>Beschreibung</lab><textarea id="fB" placeholder="Details ..."></textarea>'
    +'<lab>Ablauf</lab><textarea id="fPr" style="min-height:80px;font-family:monospace;font-size:12px" placeholder="1. Schritt 1&#10;2. Schritt 2"></textarea>'
    +'<div class="r2"><div><lab>Start</lab><input type="date" id="fS"></div><div><lab>Ziel</lab><input type="date" id="fD"></div></div>'
    +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div><lab>Aufwand (min)</lab><input type="number" id="fE" min="0" step="5"></div><div><lab>Priorit&auml;t</lab><select id="fP"><option value="hoch">Hoch</option><option value="mittel" selected>Mittel</option><option value="niedrig">Niedrig</option></select></div></div>'
    +'<lab>Projekte</lab><div id="fPW" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surf3);color:var(--text);min-height:60px"></div>'
    +'<input type="text" id="fPN" style="width:100%;margin-top:4px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surf3);color:var(--text)" placeholder="Neues Projekt (Komma getrennt) ...">'
    +'<lab>Status</lab><select id="fSt"><option value="backlog">Backlog</option><option value="ready" selected>Bereit</option><option value="running">In Arbeit</option><option value="blocked">Blockiert</option></select>'
    +'<div class="br"><button class="btn btn-g" onclick="cMo()">Abbrechen</button><button class="btn btn-p" onclick="cTa()">Erstellen</button></div>';
  const pj=[...new Set(T.flatMap(t=>gP(t)).filter(Boolean))].sort();
  const pw=document.getElementById('fPW');
  if(pw)pw.innerHTML=pj.length?pj.map(p=>'<label style="display:flex;align-items:center;gap:4px;margin:2px 0;font-size:12px;cursor:pointer"><input type="checkbox" value="'+esc(p)+'"> '+esc(p)+'</label>').join(''):'<span style="color:var(--faint);font-size:11px">Keine Projekte</span>';
  document.getElementById('mO').classList.add('show');
  setTimeout(()=>document.getElementById('fT')?.focus(),100);
}
function cMo(){document.getElementById('mO').classList.remove('show')}
async function cTa(){
  const ti=document.getElementById('fT').value.trim();if(!ti){alert('Titel ist Pflicht!');return}
  const ch=document.querySelectorAll('#fPW input[type=checkbox]:checked');const pj=Array.from(ch).map(c=>c.value);
  const cu=(document.getElementById('fPN')?.value||'').trim();if(cu)cu.split(',').map(s=>s.trim()).filter(Boolean).forEach(p=>{if(!pj.includes(p))pj.push(p)});
  await ap('/api/tasks',{method:'POST',body:JSON.stringify({title:ti,body:document.getElementById('fB')?.value||'',procedure:document.getElementById('fPr')?.value||'',priority:document.getElementById('fP')?.value||'mittel',projects:pj,status:document.getElementById('fSt')?.value||'ready',start_date:document.getElementById('fS')?.value||'',due_date:document.getElementById('fD')?.value||'',estimated_minutes:parseInt(document.getElementById('fE')?.value)||0})});
  cMo();await lT()
}
async function eT(id){
  const t=T.find(x=>x.id==id);if(!t)return
  const p=pL(t.priority);
  document.getElementById('mT').textContent='Aufgabe bearbeiten';
  document.getElementById('mB').innerHTML='<lab>Titel</lab><input type="text" id="fT" value="'+esc(t.title)+'">'
    +'<lab>Beschreibung</lab><textarea id="fB">'+esc(t.body||'')+'</textarea>'
    +'<lab>Ablauf</lab><textarea id="fPr" style="min-height:80px;font-family:monospace;font-size:12px">'+esc(t.procedure||'')+'</textarea>'
    +'<div class="r2"><div><lab>Start</lab><input type="date" id="fS" value="'+(t.started_at?new Date(t.started_at*1000).toISOString().split('T')[0]:'')+'"></div><div><lab>Ziel</lab><input type="date" id="fD" value="'+(t.completed_at?new Date(t.completed_at*1000).toISOString().split('T')[0]:'')+'"></div></div>'
    +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div><lab>Aufwand (min)</lab><input type="number" id="fE" value="'+(t.estimated_minutes||0)+'" min="0" step="5"></div><div><lab>Priorit&auml;t</lab><select id="fP"><option value="hoch" '+(p==='hoch'?'selected':'')+'>Hoch</option><option value="mittel" '+(p==='mittel'?'selected':'')+'>Mittel</option><option value="niedrig" '+(p==='niedrig'?'selected':'')+'>Niedrig</option></select></div></div>'
    +'<lab>Projekte</lab><div id="fPW" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surf3);color:var(--text);min-height:60px"></div>'
    +'<input type="text" id="fPN" style="width:100%;margin-top:4px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surf3);color:var(--text)" placeholder="Neues Projekt">'
    +'<lab>Status</label><select id="fSt">'+Co.map(s=>'<option value="'+s+'" '+(s===t.status?'selected':'')+'>'+Cn[s]+'</option>').join('')+'</select>'
    +'<div class="br"><button class="btn btn-g" onclick="cMo()">Abbrechen</button><button class="btn btn-p" onclick="sT('+"'"+t.id+"'"+')">Speichern</button><button class="btn btn-g" onclick="dT('+"'"+t.id+"'"+')">L&ouml;schen</button></div>';
  const pj=[...new Set(T.flatMap(t=>gP(t)).filter(Boolean))].sort();const cur=gP(t);
  const pw=document.getElementById('fPW');
  if(pw)pw.innerHTML=pj.length?pj.map(p=>'<label style="display:flex;align-items:center;gap:4px;margin:2px 0;font-size:12px;cursor:pointer"><input type="checkbox" value="'+esc(p)+'" '+(cur.includes(p)?'checked':'')+'> '+esc(p)+'</label>').join(''):'<span style="color:var(--faint)">Keine Projekte</span>';
  document.getElementById('mO').classList.add('show');
}
async function sT(id){
  const ti=document.getElementById('fT').value.trim();if(!ti){alert('Titel ist Pflicht!');return}
  const ch=document.querySelectorAll('#fPW input[type=checkbox]:checked');const pj=Array.from(ch).map(c=>c.value);
  const cu=(document.getElementById('fPN')?.value||'').trim();if(cu)cu.split(',').map(s=>s.trim()).filter(Boolean).forEach(p=>{if(!pj.includes(p))pj.push(p)});
  await ap('/api/tasks/'+id,{method:'PUT',body:JSON.stringify({title:ti,body:document.getElementById('fB')?.value||'',procedure:document.getElementById('fPr')?.value||'',priority:document.getElementById('fP')?.value||'mittel',projects:pj,start_date:document.getElementById('fS')?.value||'',due_date:document.getElementById('fD')?.value||'',estimated_minutes:parseInt(document.getElementById('fE')?.value)||0})});
  cMo();await lT()
}

// Calendar
let cOff=0;
function cN(d){cOff+=d;rCal()}
function cT(){cOff=0;rCal()}
function rCal(){
  const n=new Date();n.setDate(n.getDate()+cOff*7);
  const sd=n.getDay(), d=n.getDate()-(sd===0?6:sd-1)+cOff*7;
  const start=new Date(n.setDate(d));start.setHours(0,0,0,0);
  const days=Array.from({length:7},(_,i)=>{const d=new Date(start);d.setDate(d.getDate()+i);return d});
  const fmt=d=>d.toLocaleDateString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit'});
  const iT=d=>{const t=new Date();return d.getFullYear()===t.getFullYear()&&d.getMonth()===t.getMonth()&&d.getDate()===t.getDate()};
  document.getElementById('cLab').textContent=fmt(days[0])+' - '+fmt(days[6]);
  const dTs=days.map(d=>Math.floor(d.getTime()/1000));
  const nDTs=days.map(d=>Math.floor(new Date(d.getFullYear(),d.getMonth(),d.getDate()+1).getTime()/1000));
  const byDay=days.map(()=>[]);
  const cf=document.getElementById('cFil').value;const ct=cf?T.filter(t=>gP(t).includes(cf)):T;
  const cd=ct.filter(t=>t.started_at||t.completed_at);
  cd.forEach(t=>{const s=t.started_at||t.created_at;const e=t.completed_at||s+86400*14;if(!s)return;for(let i=0;i<7;i++)if(s<nDTs[i]&&e>=dTs[i])byDay[i].push(t)});
  let h='<table style="width:100%;border-collapse:collapse;table-layout:fixed;min-width:700px"><thead><tr>';
  days.forEach((d,i)=>{const bs=iT(d)?' style="background:var(--accent);color:#0a0e17;border-radius:6px 6px 0 0"':'';h+='<th'+bs+' style="padding:8px 6px;font-size:12px;text-align:center;font-weight:600">'+fmt(d)+'</th>'});
  h+='</tr></thead><tbody><tr>';
  days.forEach((d,i)=>{const bs=iT(d)?' style="background:var(--glow)"':'';h+='<td'+bs+' style="vertical-align:top;padding:4px;border:1px solid var(--border);height:120px;width:14.28%">';
  byDay[i].forEach(t=>{const p=pL(t.priority);h+='<div style="background:'+(Cl[p]||'#999')+';color:#fff;border-radius:4px;padding:2px 4px;margin-bottom:2px;font-size:10px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'+(t.status==='completed'?'opacity:0.5':'')+'" onclick="eT('+"'"+t.id+"'"+')">'+esc(t.title)+'</div>'});
  h+='</td>'});
  h+='</tr></tbody></table>';
  document.getElementById('calW').innerHTML=h;
}

// Gantt
function rGan(){
  const f=document.getElementById('gFil').value;const ft=f?T.filter(t=>gP(t).includes(f)):T;
  const wd=ft.filter(t=>t.started_at||t.completed_at);
  if(!wd.length){document.getElementById('ganW').innerHTML='<div class="em2">Keine Aufgaben mit Datum</div>';return}
  wd.sort((a,b)=>(a.started_at||0)-(b.started_at||0));
  let h='<table class="gt"><thead><tr><th>Titel</th><th>Projekt</th><th>Priorit&auml;t</th><th>Start</th><th>Ziel</th><th>Aufwand</th></tr></thead><tbody>';
  wd.forEach(t=>{const p=pL(t.priority);h+='<tr onclick="eT('+"'"+t.id+"'"+')" style="cursor:pointer"><td style="font-weight:600">'+esc(t.title)+'</td><td style="color:var(--dim);font-size:12px">'+esc(gP(t).join(', '))+'</td><td><span style="color:'+(Cl[p]||'#999')+';font-weight:600">'+p+'</span></td><td style="font-size:12px">'+(t.started_at?new Date(t.started_at*1000).toLocaleDateString('de-DE'):'')+'</td><td style="font-size:12px">'+(t.completed_at?new Date(t.completed_at*1000).toLocaleDateString('de-DE'):'')+'</td><td style="font-size:12px">'+(t.estimated_minutes?fM(t.estimated_minutes):'')+'</td></tr>'});
  h+='</tbody></table>';
  // Timeline
  h+='<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border2)"><div style="font-size:11px;font-weight:600;color:var(--dim);margin-bottom:6px">Zeitstrahl</div>';
  const now=Date.now()/1000;
  if(wd.length){const mn=Math.min(...wd.map(t=>t.started_at||t.created_at));const mx=Math.max(...wd.map(t=>t.completed_at||now));const r=Math.max(mx-mn,86400);
  wd.slice(0,15).forEach(t=>{const s=t.started_at||t.created_at;const e=t.completed_at||(s+86400*14);const l=((s-mn)/r*100);const w=Math.max(((e-s)/r*100),1);
  h+='<div style="display:flex;align-items:center;margin-bottom:4px"><div class="tl" style="width:140px;font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:8px;flex-shrink:0">'+esc(t.title)+'</div><div style="flex:1;height:24px;background:var(--surf3);border-radius:4px;position:relative;overflow:hidden"><div style="position:absolute;top:0;left:'+l+'%;width:'+w+'%;height:100%;border-radius:4px;min-width:4px;background:'+(Cl[pL(t.priority)]||'#999')+'"></div></div></div>'})}
  h+='</div>';
  document.getElementById('ganW').innerHTML=h;
}

// Pomodoro
function tPo(){document.getElementById('pO').classList.toggle('show')}
function pSt(){
  if(pS==='running'){pS='paused';clearInterval(pI);document.getElementById('pBtn').textContent='Weiter'}
  else{
    pS='running';document.getElementById('pBtn').textContent='Pause';
    if(pR<=0)pR=25*60;
    pI=setInterval(()=>{pR--;if(pR<=0){clearInterval(pI);pS='break';pR=5*60;pC++;localStorage.setItem('pC',pC);uPD();document.getElementById('pBtn').textContent='Start'}uPD()},1000)
  }
  uPD()
}
function pRe(){clearInterval(pI);pS='idle';pR=25*60;document.getElementById('pBtn').textContent='Start';uPD()}
function uPD(){const m=Math.floor(pR/60),s=pR%60;document.getElementById('pTi').textContent=m.toString().padStart(2,'0')+':'+s.toString().padStart(2,'0');document.getElementById('pPh').textContent=pS==='break'?'Pause':pS==='paused'?'Pausiert':'Fokus';document.getElementById('pCo').textContent=pC+' Today'}

// Tabs
function swT(n){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  const tab=document.querySelector('.tab[onclick*="'+n+'"]');if(tab)tab.classList.add('active');
  const p=document.getElementById('p-'+n);if(p)p.classList.add('active');
  if(n==='dash')rDa();if(n==='kan')rKan();if(n==='gan')rGan();if(n==='cal')rCal()
}
function tTh(){localStorage.setItem('lt',document.getElementById('thT').checked?'dark':'light')}

// Init
if(localStorage.getItem('lt')==='light'){document.body.style.background='#fff';document.body.style.color='#111';document.getElementById('thT').checked=false}
lT();setInterval(lT,60000)

async function aRev(){
  const s=document.getElementById('revSi').value.trim();
  const a=parseFloat(document.getElementById('revAi').value);
  if(!s||!a)return;
  await ap('/api/revenue',{method:'POST',body:JSON.stringify({source:s,amount:a})});
  document.getElementById('revSi').value='';document.getElementById('revAi').value='';
  const rd=await ap('/api/revenue');
  document.getElementById('revT').innerHTML=Number(rd.total).toLocaleString('de-DE',{minimumFractionDigits:2})+' €';
  const co=document.getElementById('revS');
  const ss=rd.by_source||[];
  co.innerHTML=ss.length?ss.map(function(sv){return '<div class="rs"><span class="rn">'+esc(sv.source)+'</span><span class="ra">'+Number(sv.total).toFixed(2).replace('.',',')+' €</span></div>'}).join(''):'<div class="em2">Keine</div>';
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), KanbanHandler)
    print(f"✅ Kanban + Gantt + Brainstorming + Pomodoro: http://{HOST}:{PORT}")
    print(f"   Dashboard:                                 http://{HOST}:{PORT}/")
    print(f"   API:                                       http://{HOST}:{PORT}/api/tasks")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\nServer gestoppt.")