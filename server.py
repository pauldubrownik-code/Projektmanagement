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
        elif path == "/api/brainstorm":
            self.send_json({"entries": fetch_brainstorms()})
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
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Projekte · Paul Dubrownik</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#f4f6f8;--surface:#fff;--text:#1a1a2e;--text-muted:#666;--text-medium:#555;--text-dim:#888;--text-faint:#999;--text-very-dim:#aaa;--text-strong:#333;--primary:#002D69;--primary-hover:#003d8f;--accent:#007FA7;--column-bg:#e5e7eb;--border:#ddd;--border-light:#e5e7eb;--hover-bg:#e5e7eb;--hover-bg2:#f0f0f0;--card-shadow:rgba(0,0,0,.08);--modal-overlay:rgba(0,0,0,.5);--pomo-overlay:rgba(0,0,0,.85);--pomo-shadow:rgba(124,58,237,.4);--danger:#DC2626;--danger-hover:#B91C1C;--success:#059669;--success-hover:#047857;--purple:#7C3AED;--purple-hover:#6D28D9;--prio-high:#DD3221;--prio-med:#f59e0b;--prio-low:#6b7280;--input-bg:#fff;--input-border:#ddd;--gantt-bg:#fff;--timeline-bg:#f0f0f0;--proc-bg:#f8f0ff;--drag-over-bg:#dbeafe}
.dark-mode{--bg:#0f172a;--surface:#1e293b;--text:#e2e8f0;--text-muted:#94a3b8;--text-medium:#94a3b8;--text-dim:#64748b;--text-faint:#475569;--text-very-dim:#334155;--text-strong:#94a3b8;--primary:#3b82f6;--primary-hover:#2563eb;--accent:#38bdf8;--column-bg:#1e293b;--border:#334155;--border-light:#1e293b;--hover-bg:#334155;--hover-bg2:#334155;--card-shadow:rgba(0,0,0,.4);--modal-overlay:rgba(0,0,0,.7);--pomo-overlay:rgba(0,0,0,.9);--pomo-shadow:rgba(59,130,246,.4);--purple:#818cf8;--purple-hover:#6366f1;--input-bg:#1e293b;--input-border:#334155;--gantt-bg:#1e293b;--timeline-bg:#334155;--proc-bg:#1e1b3a;--drag-over-bg:#1e3a5f}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:20px}
h1{font-size:22px;color:var(--primary);margin-bottom:4px}
h1 span{color:var(--accent);font-weight:400}
.sub{font-size:13px;color:var(--text-dim);margin-bottom:16px}
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:2px solid var(--border-light)}
.tab{padding:10px 20px;cursor:pointer;font-size:14px;font-weight:600;background:transparent;color:var(--text-dim);border:none;border-bottom:2px solid transparent;margin-bottom:-2px;transition:.2s}
.tab:hover{color:var(--primary)}
.tab.active{color:var(--primary);border-bottom-color:var(--primary)}
.panel{display:none}
.panel.active{display:block}
.toolbar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.btn{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:.2s;display:inline-flex;align-items:center;gap:4px;font-family:inherit}
.btn-primary{background:var(--primary);color:#fff}
.btn-primary:hover{background:var(--primary-hover)}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-danger{background:var(--danger);color:#fff}
.btn-danger:hover{background:var(--danger-hover)}
.btn-ghost{background:transparent;color:var(--text-muted)}
.btn-ghost:hover{background:var(--border-light);color:var(--text-strong)}
.btn-success{background:var(--success);color:#fff}
.btn-success:hover{background:var(--success-hover)}
.btn-pomo{background:var(--purple);color:#fff}
.btn-pomo:hover{background:var(--purple-hover)}

/* Kanban */
.kanban{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;min-height:60vh}
.column{background:var(--border-light);border-radius:10px;padding:10px;min-height:200px;display:flex;flex-direction:column}
.col-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:2px solid var(--border);font-size:12px;color:var(--text-muted)}
.col-header h3{font-size:13px;font-weight:600}
.col-count{background:var(--primary);color:#fff;font-size:10px;border-radius:10px;padding:1px 7px;font-weight:600}
.col-body{flex:1;min-height:100px}
.card{background:var(--surface);border-radius:8px;padding:12px;margin-bottom:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);cursor:pointer;transition:.2s;border-left:4px solid var(--text-faint);position:relative}
.card:hover{box-shadow:0 2px 8px rgba(0,0,0,.12)}
.card.prio-hoch{border-left-color:var(--prio-high)}
.card.prio-mittel{border-left-color:var(--prio-med)}
.card.prio-niedrig{border-left-color:var(--prio-low)}
.card.dragging{opacity:0.4;transform:rotate(2deg)}
.col-body.drag-over{background:var(--drag-over-bg);border-radius:8px;min-height:60px}
.card-proc{margin-top:6px;font-size:11px}
.card-proc-toggle{color:var(--purple);cursor:pointer;font-weight:500;font-size:10px;user-select:none}
.card-proc-toggle:hover{text-decoration:underline}
.card-proc-body{display:none;margin-top:4px;padding:6px 8px;background:var(--proc-bg);border-radius:4px;color:#444;font-size:11px;line-height:1.5;white-space:pre-wrap}
.card-proc.expanded .card-proc-body{display:block}
.card-title{font-size:13px;font-weight:600;color:var(--text);margin-bottom:2px;padding-right:50px}
.card-body{font-size:11px;color:var(--text-muted);line-height:1.3;margin-top:4px}
.card-meta{font-size:10px;color:var(--text-faint);margin-top:6px;display:flex;gap:8px;flex-wrap:wrap}
.card-dates{font-size:9px;color:var(--text-very-dim);margin-top:4px;display:flex;gap:6px}
.card-est{font-size:9px;color:var(--purple);margin-top:2px;font-weight:500}
.card-actions{position:absolute;top:6px;right:6px;display:flex;gap:2px}
.card-actions button{background:none;border:none;cursor:pointer;font-size:14px;color:var(--text-faint);padding:2px 4px;border-radius:4px;transition:.2s}
.card-actions button:hover{background:var(--hover-bg2);color:var(--text-strong)}
.status-select{font-size:10px;padding:2px 4px;border:1px solid var(--border);border-radius:4px;background:var(--surface);cursor:pointer;color:#555}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:1000;justify-content:center;align-items:center;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:var(--surface);border-radius:12px;padding:24px;max-width:600px;width:100%;max-height:85vh;overflow-y:auto;box-shadow:0 4px 24px rgba(0,0,0,.2);animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.modal-close{float:right;font-size:22px;cursor:pointer;color:var(--text-faint);line-height:1}
.modal-close:hover{color:var(--text-strong)}
.modal h2{color:var(--primary);font-size:18px;margin-bottom:16px;padding-right:30px}
.modal label{display:block;font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:4px;margin-top:12px}
.modal input[type=text],.modal input[type=date],.modal input[type=number],.modal textarea,.modal select{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;font-family:inherit}
.modal textarea{min-height:80px;resize:vertical}
.modal .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.modal .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.modal .btn-row{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}

/* Gantt */
.gantt{overflow-x:auto;background:var(--surface);border-radius:10px;padding:16px}
.gantt-table{width:100%;border-collapse:collapse;font-size:13px;min-width:750px}
.gantt-table th{background:var(--primary);color:#fff;padding:8px 12px;text-align:left;font-size:12px;position:sticky;top:0}
.gantt-table td{padding:8px 12px;border-bottom:1px solid var(--hover-bg2);vertical-align:middle}
.gantt-date{font-size:10px;color:var(--text-faint);white-space:nowrap}
.gantt-timeline{position:relative;margin-top:16px;padding-top:12px;border-top:1px solid var(--border-light)}
.gantt-timeline .tl-label{font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:6px}
.timeline-row{display:flex;align-items:center;margin-bottom:4px;position:relative}
.timeline-label{width:140px;font-size:12px;font-weight:500;color:var(--text-strong);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:8px;flex-shrink:0}
.timeline-track{flex:1;height:24px;background:var(--hover-bg2);border-radius:4px;position:relative;overflow:hidden}
.timeline-fill{position:absolute;top:0;left:0;height:100%;border-radius:4px;min-width:4px;transition:width .5s}
.timeline-text{position:absolute;top:0;left:4px;height:100%;display:flex;align-items:center;font-size:10px;color:#fff;font-weight:600;white-space:nowrap}

/* Misc */

.dark-mode .gantt{background:var(--surface)}

/* Dark mode toggle */
.theme-fab{position:fixed;bottom:24px;right:88px;width:40px;height:40px;border-radius:50%;background:var(--surface);color:var(--text);border:2px solid var(--border);cursor:pointer;font-size:18px;box-shadow:0 2px 8px var(--card-shadow);z-index:900;transition:.2s;display:flex;align-items:center;justify-content:center}
.theme-fab:hover{transform:scale(1.1);border-color:var(--purple);color:var(--purple)}

/* Pomodoro */
.pomo-fab{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:var(--purple);color:#fff;border:none;cursor:pointer;font-size:24px;box-shadow:0 4px 16px rgba(124,58,237,.4);z-index:900;transition:.2s}
.pomo-fab:hover{transform:scale(1.1);background:var(--purple-hover)}
.pomo-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.85);z-index:9998;justify-content:center;align-items:center;flex-direction:column}
.pomo-overlay.show{display:flex}
.pomo-card{background:var(--surface);border-radius:20px;padding:40px;text-align:center;max-width:360px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.3)}
.pomo-card .phase{font-size:14px;font-weight:600;color:var(--purple);margin-bottom:8px}
.pomo-card .timer{font-size:72px;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;margin:16px 0}
.pomo-card .timer.pause{color:var(--prio-med)}
.pomo-card .timer.break{color:var(--success)}
.pomo-card .pomo-btn-row{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.pomo-card .pomo-btn-row button{min-width:80px}
.pomo-card .task-label{font-size:12px;color:var(--text-dim);margin-top:12px;padding:8px;background:#f4f6f8;border-radius:8px}
.pomo-card .close-pomo{position:absolute;top:12px;right:12px;font-size:24px;cursor:pointer;color:var(--text-faint);background:none;border:none}
.pomo-card-wrap{position:relative}
.pomo-count{font-size:11px;color:var(--text-very-dim);margin-top:8px}

/* Misc */
.empty-state{padding:40px 20px;text-align:center;color:var(--text-very-dim);font-size:14px}
.loading{text-align:center;padding:40px;color:var(--text-dim);font-size:14px}
.spinner{display:inline-block;width:20px;height:20px;border:3px solid var(--border-light);border-top-color:var(--primary);border-radius:50%;animation:spin .6s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:1000px){.kanban{grid-template-columns:repeat(3,1fr)}.brainstorm-panel{grid-template-columns:1fr}}
@media(max-width:700px){.kanban{grid-template-columns:repeat(2,1fr)}.timeline-label{width:100px;font-size:11px}}
@media(max-width:500px){.kanban{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>Projekte <span>· Paul Dubrownik</span></h1>
<div class="sub" id="statusLine">Lade Daten …</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('overview')">📊 Übersicht</button>
  <button class="tab" onclick="switchTab('kanban')">📋 Kanban</button>
  <button class="tab" onclick="switchTab('calendar')">📅 Kalender</button>
  <button class="tab" onclick="switchTab('gantt')">📊 Gantt</button>
</div>

<div id="panel-kanban" class="panel">
  <div class="toolbar">
    <button class="btn btn-primary" onclick="openCreateModal()">+ Neue Karte</button>
    <button class="btn btn-ghost" onclick="openImportModal()">📥 Import</button>
    <select id="projectFilter" onchange="renderKanban()" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;background:var(--surface)">
      <option value="">📁 Alle Projekte</option>
    </select>
    <div style="position:relative;flex:1;min-width:160px">
      <input id="smartInput" type="text" style="width:100%;padding:6px 30px 6px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;background:var(--input-bg);color:var(--text)" placeholder="🧠 'Klarna (5min)' ⏎" onkeydown="if(event.key==='Enter')smartAdd(event.target.value)">
    </div>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="kanban" class="kanban"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<div id="panel-gantt" class="panel">
  <div class="toolbar">
    <select id="ganttProjectFilter" onchange="renderGantt()" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;background:var(--surface)">
      <option value="">📁 Alle Projekte</option>
    </select>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="ganttWrap" class="gantt"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<div id="panel-overview" class="panel active">
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="overviewWrap"><div class="loading"><span class="spinner"></span>Lade …</div></div>
  <div id="routinesOverview"></div>
</div>

<div id="panel-calendar" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="calNav(-1)">◀</button>
    <span id="calLabel" style="font-size:14px;font-weight:600;color:var(--primary);min-width:240px;text-align:center"></span>
    <button class="btn btn-ghost" onclick="calNav(1)">▶</button>
    <button class="btn btn-ghost" onclick="calToday()">Heute</button>
    <select id="calProjectFilter" onchange="renderCalendar()" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;background:var(--surface)">
      <option value="">📁 Alle Projekte</option>
    </select>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄</button>
    <span style="flex:1"></span>
    <button class="btn btn-sm cal-vw" data-vw="week" onclick="calSetView('week')" style="background:var(--primary);color:#fff">Woche</button>
    <button class="btn btn-sm cal-vw" data-vw="day" onclick="calSetView('day')">Tag</button>
    <button class="btn btn-sm cal-vw" data-vw="month" onclick="calSetView('month')">Monat</button>
    <button class="btn btn-sm cal-vw" data-vw="year" onclick="calSetView('year')">Jahr</button>
  </div>
  <div id="calendarWrap" style="background:var(--surface);border-radius:10px;padding:16px;overflow-x:auto">
    <div class="loading"><span class="spinner"></span>Lade …</div>
  </div>
</div>

<!-- Modal -->
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modalContent">
    <span class="modal-close" onclick="closeModal()">&times;</span>
    <h2 id="modalTitle">Aufgabe</h2>
    <div id="modalBody"></div>
  </div>
</div>

<!-- Smart AI Import Modal -->
<div class="modal-overlay" id="importOverlay" onclick="if(event.target===this)closeImportModal()">
  <div class="modal-content" style="max-width:600px;max-height:80vh;overflow-y:auto">
    <span class="modal-close" onclick="closeImportModal()">&times;</span>
    <h2>🧠 KI-Smart-Import</h2>
    <p style="font-size:12px;color:var(--text-dim);margin:0 0 6px">Einfach Text reinpasten — KI erkennt <strong>Projekt, Priorität, Dauer & Status</strong> automatisch.</p>
    <p style="font-size:11px;color:var(--text-dim);margin:0 0 10px">🔍 Z.B. <em>"Dringend: Klarna Rechnung bezahlen (5min)"</em> → hoch Prio, 5min, Finanzen &amp; Admin. <em>Mit ### für Kategorien, * für Aufgaben.</em></p>
    <textarea id="importText" style="width:100%;min-height:250px;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:monospace;background:var(--input-bg);color:var(--text);resize:vertical" placeholder="### 💼 Beruf & Schule
* Dringend: Mit Chef über Gehaltserhöhung sprechen (30min)
* !! Arbeitsstunden-System entwickeln (2h)
* [blocked] Danju Website — warte auf Feedback

### 🔴 Finanzen
* Klarna Rechnungen bezahlen (5min)
* MagentaTV kündigen!!"></textarea>
    <div id="importPreview" style="display:none;margin:8px 0;font-size:12px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px;max-height:200px;overflow-y:auto"></div>
    <div id="importProgress" style="display:none;font-size:12px;margin:6px 0"></div>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="closeImportModal()">Abbrechen</button>
      <button class="btn btn-secondary" onclick="previewImport()">🔍 Vorschau</button>
      <button class="btn btn-primary" onclick="runImport()">🧠 KI-Import</button>
    </div>
  </div>
</div>

<!-- Theme Toggle + Pomodoro FAB -->
<button class="theme-fab" id="themeFab" onclick="toggleTheme()" title="Dark/Light Mode">🌙</button>
<button class="pomo-fab" id="pomoFab" onclick="togglePomo()" title="Fokus-Modus (Pomodoro)">🍅</button>

<!-- Pomodoro Overlay -->
<div class="pomo-overlay" id="pomoOverlay">
  <div class="pomo-card-wrap">
    <div class="pomo-card">
      <button class="close-pomo" onclick="togglePomo()">&times;</button>
      <div class="phase" id="pomoPhase">🍅 Fokus</div>
      <div class="timer" id="pomoTimer">25:00</div>
      <div class="pomo-btn-row">
        <button class="btn btn-pomo" id="pomoBtn" onclick="pomoStart()">▶ Start</button>
        <button class="btn btn-ghost" onclick="pomoReset()">↺ Reset</button>
      </div>
      <div class="task-label" id="pomoTask">Keine Aufgabe ausgewählt</div>
      <div class="pomo-count" id="pomoCount">🍅 0 Today</div>
    </div>
  </div>
</div>

<script>
// ── State ──
let allTasks = [];

// ── Pomodoro State ──
let pomoState = 'idle'; // idle | running | paused | break
let pomoRemaining = 25 * 60;
let pomoInterval = null;
let pomoCountToday = parseInt(localStorage.getItem('pomoCount') || '0');
let pomoDate = localStorage.getItem('pomoDate') || '';

// Check date reset
const today = new Date().toDateString();
if (pomoDate !== today) {
  pomoCountToday = 0;
  localStorage.setItem('pomoDate', today);
}
localStorage.setItem('pomoCount', pomoCountToday);

// ── API ──
async function api(path, opts={}) {
  const res = await fetch(path, {
    headers: {'Content-Type':'application/json', ...opts.headers},
    ...opts
  });
  return res.json();
}

// ── Load ──
async function loadTasks() {
  document.getElementById('statusLine').textContent = 'Lade …';
  try {
    const [td, rd] = await Promise.all([
      api('/api/tasks'),
      api('/api/routines')
    ]);
    allTasks = td.tasks || [];
    routines = rd.routines || [];
    document.getElementById('statusLine').textContent =
      allTasks.length + ' Aufgaben · ' + new Date().toLocaleTimeString('de-DE');

    // Update project filters
    const projs = [...new Set(allTasks.flatMap(t => getProjs(t)).filter(Boolean))].sort();
    const opts = projs.map(p => `<option value="${escHtml(p)}">${escHtml(p)}</option>`).join('');
    const filterSel = document.getElementById('projectFilter');
    const currentVal = filterSel.value;
    filterSel.innerHTML = '<option value="">📁 Alle Projekte</option>' + opts;
    filterSel.value = currentVal;
    const ganttSel = document.getElementById('ganttProjectFilter');
    const ganttVal = ganttSel.value;
    ganttSel.innerHTML = '<option value="">📁 Alle Projekte</option>' + opts;
    ganttSel.value = ganttVal;
    const calSel = document.getElementById('calProjectFilter');
    const calVal = calSel.value;
    calSel.innerHTML = '<option value="">📁 Alle Projekte</option>' + opts;
    calSel.value = calVal;

    renderKanban();
    renderGantt();
    renderOverview();
    renderCalendar();
  } catch(e) {
    document.getElementById('statusLine').textContent = '⚠️ Fehler beim Laden';
    console.error(e);
  }
}

// ── Helpers ──
function PRIO_LABEL(p) {
  if (typeof p === 'number') return {1:'hoch',2:'mittel',3:'niedrig'}[p]||'mittel';
  if (['hoch','mittel','niedrig'].includes(p)) return p;
  return 'mittel';
}
// Escapt auch Anführungszeichen, damit Werte in HTML-Attributen
// (z.B. Projektfilter-Optionen) nicht abgeschnitten werden.
function escHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function tsToDate(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleDateString('de-DE'); } catch(e) { return ''; }
}
function tsToDateInput(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toISOString().split('T')[0]; } catch(e) { return ''; }
}
function fmtMinutes(m) {
  if (!m || m <= 0) return '';
  if (m >= 60) return Math.floor(m/60)+'h '+ (m%60 ? m%60+'min' : '');
  return m + 'min';
}

// ── Kanban ──
const COLS = ['backlog','ready','running','completed','blocked'];
const COL_NAMES = {'backlog':'📋 Backlog','ready':'🟡 Bereit','running':'🟢 In Arbeit','completed':'✅ Erledigt','blocked':'🔴 Blockiert'};
const COL_NAMES_SHORT = {'backlog':'Backlog','ready':'Bereit','running':'In Arbeit','completed':'Erledigt','blocked':'Blockiert'};
const PRIO_C = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};

// Karten mit unbekanntem Status (z.B. von externen Writern in der DB)
// dürfen nicht aus dem Board verschwinden — sie landen im Backlog.
function kanbanCol(t) {
  return COLS.includes(t.status) ? t.status : 'backlog';
}

function getProjs(t){
  try{const p=JSON.parse(t.project_ids||'[]');return Array.isArray(p)?p:[]}catch(e){return []}
}
function getProjStr(t){return getProjs(t).filter(Boolean).join(', ')||'(ohne Projekt)'}

function renderKanban() {
  const filter = document.getElementById('projectFilter').value;
  const kanban = document.getElementById('kanban');
  const filtered = filter ? allTasks.filter(t => getProjs(t).includes(filter)) : allTasks;
  kanban.innerHTML = COLS.map(col => {
    const items = filtered.filter(t => kanbanCol(t) === col);
    return `<div class="column">
      <div class="col-header">
        <h3>${COL_NAMES[col]}</h3>
        <span class="col-count">${items.length}</span>
      </div>
      <div class="col-body" data-col="${col}" ondragover="onDragOver(event)" ondrop="onDrop(event)" ondragleave="onDragLeave(event)">
        ${items.length ? items.map(t => renderCard(t)).join('') :
          '<div class="empty-state">— leer —</div>'}
      </div>
    </div>`;
  }).join('');
}

function renderCard(t) {
  const prio = PRIO_LABEL(t.priority);
  const bodyShort = t.body ? t.body.substring(0, 80) + (t.body.length > 80 ? '…' : '') : '';
  const startStr = tsToDate(t.started_at);
  const dueStr = tsToDate(t.completed_at);
  const datesHtml = (startStr || dueStr)
    ? `<div class="card-dates">${startStr ? '📅 '+startStr : ''} ${dueStr ? '→ '+dueStr : ''}</div>`
    : '';
  const estHtml = t.estimated_minutes > 0
    ? `<div class="card-est">🕐 ${fmtMinutes(t.estimated_minutes)}${t.buffer_percent ? ' +'+t.buffer_percent+'% Puffer' : ''}</div>`
    : '';
  const projs2 = getProjs(t);
  const projHtml = projs2.length ? projs2.map(p => `<span style="background:#e5e7eb;border-radius:3px;padding:1px 5px;font-size:9px;color:var(--text-medium);margin:0 1px">📁 ${escHtml(p)}</span>`).join('') : '';
  return `<div class="card prio-${prio}" data-task-id="${t.id}" draggable="true" ondragstart="onDragStart(event)" onclick="editTask('${t.id}')">
    <div class="card-actions" onclick="event.stopPropagation()">
      <button onclick="event.stopPropagation();startPomoForTask('${t.id}')" title="Fokus">🍅</button>
      <button onclick="deleteTask('${t.id}')" title="Löschen">🗑</button>
    </div>
    <div class="card-title">${escHtml(t.title)}</div>
    ${bodyShort ? `<div class="card-body">${escHtml(bodyShort)}</div>` : ''}
    ${t.procedure ? `<div class="card-proc"><span class="card-proc-toggle" onclick="event.stopPropagation();this.parentElement.classList.toggle('expanded')">📋 Ablauf <small>(${t.procedure.split('\n').filter(l=>l.trim()).length} Schritte)</small></span><div class="card-proc-body">${escHtml(t.procedure).replace(/\n/g,'<br>')}</div></div>` : ''}
    ${estHtml}
    ${datesHtml}
    <div class="card-meta">
      <span style="color:${PRIO_C[prio]||'#999'};font-weight:600">${prio}</span>
      ${projHtml}
      <select class="status-select" autocomplete="off" onclick="event.stopPropagation()">
        ${COLS.includes(t.status) ? '' : `<option value="" selected>⚠ ${escHtml(t.status||'—')}</option>`}
        ${COLS.map(s => `<option value="${s}" ${s===t.status?'selected':''}>${COL_NAMES_SHORT[s]||s}</option>`).join('')}
      </select>
    </div>
  </div>`;
}

// ── Status + Delete ──
async function changeStatus(id, newStatus) {
  if (!COLS.includes(newStatus)) return;
  const t = allTasks.find(x => x.id === id);
  if (t && t.status === newStatus) return;
  try {
    await api('/api/tasks/'+id+'/status', {method:'POST', body:JSON.stringify({status:newStatus})});
  } finally {
    await loadTasks();
  }
}

// Delegierter Listener statt Inline-onchange: reagiert nur auf echte
// Nutzer-Interaktion (isTrusted), nicht auf synthetische Events von
// Extensions/Skripten.
document.getElementById('kanban').addEventListener('change', (e) => {
  if (!e.isTrusted) return;
  const sel = e.target.closest('.status-select');
  if (!sel) return;
  const card = sel.closest('.card');
  if (card && card.dataset.taskId) changeStatus(card.dataset.taskId, sel.value);
});
async function deleteTask(id) {
  if (!confirm('Aufgabe löschen?')) return;
  await api('/api/tasks/'+id, {method:'DELETE'});
  await loadTasks();
}

// ── Drag & Drop ──
function onDragStart(e) {
  e.dataTransfer.setData('text/plain', e.currentTarget.dataset.taskId);
  e.dataTransfer.effectAllowed = 'move';
  e.currentTarget.classList.add('dragging');
}
function onDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
function onDragLeave(e) {
  e.currentTarget.classList.remove('drag-over');
}
function onDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const taskId = e.dataTransfer.getData('text/plain');
  const col = e.currentTarget.dataset.col;
  if (taskId && col) {
    changeStatus(taskId, col);
  }
}
document.addEventListener('dragend', (e) => {
  e.target.classList.remove('dragging');
  document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
});

// ── Create Modal ──
function openCreateModal() {
  document.getElementById('modalTitle').textContent = 'Neue Aufgabe';
  document.getElementById('modalBody').innerHTML = `
    <label>Titel *</label>
    <input type="text" id="fTitle" placeholder="Aufgabe …" autofocus>
    <label>Beschreibung</label>
    <textarea id="fBody" placeholder="Details …"></textarea>
    <label>📋 Ablauf</label>
    <textarea id="fProcedure" placeholder="1. Erster Schritt&#10;2. Zweiter Schritt&#10;3. Dritter Schritt" style="min-height:80px;font-family:monospace;font-size:12px"></textarea>
    <div class="row2">
      <div><label>Start</label><input type="date" id="fStart"></div>
      <div><label>Ziel</label><input type="date" id="fDue"></div>
    </div>
    <div class="row3">
      <div><label>⏱ Aufwand</label><input type="number" id="fEst" placeholder="Minuten" min="0" step="5"></div>
      <div><label>📊 Puffer</label>
        <select id="fBuf">
          <option value="0">0%</option>
          <option value="10">10%</option>
          <option value="20" selected>20%</option>
          <option value="30">30%</option>
          <option value="50">50%</option>
          <option value="100">100%</option>
        </select>
      </div>
      <div><label>Priorität</label>
        <select id="fPrio">
          <option value="hoch">🔴 Hoch</option>
          <option value="mittel" selected>🟡 Mittel</option>
          <option value="niedrig">🟢 Niedrig</option>
        </select>
      </div>
    </div>
    <label>📁 Projekte</label>
    <div id="fProjectWrap" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input-bg);color:var(--text);min-height:80px"></div>
    <input type="text" id="fProjectNew" style="width:100%;margin-top:4px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input-bg);color:var(--text)" placeholder="Neues Projekt (mit Komma trennen)...">
    <label>Status</label>
    <select id="fStatus">
      <option value="backlog">📋 Backlog</option>
      <option value="ready" selected>🟡 Bereit</option>
      <option value="running">🟢 In Arbeit</option>
      <option value="blocked">🔴 Blockiert</option>
    </select>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="closeModal()">Abbrechen</button>
      <button class="btn btn-primary" onclick="createTask()">Erstellen</button>
    </div>`;
  // Populate multi-project checkboxes
  const projs = [...new Set(allTasks.flatMap(t => getProjs(t)).filter(Boolean))].sort();
  const pWrap = document.getElementById('fProjectWrap');
  if (pWrap) {
    pWrap.innerHTML = projs.map(p =>
      `<label style="display:flex;align-items:center;gap:4px;margin:2px 0;font-size:12px;cursor:pointer">
        <input type="checkbox" value="${escHtml(p)}"> 📁 ${escHtml(p)}
      </label>`
    ).join('') || '<span style="color:var(--text-dim);font-size:11px">Keine Projekte vorhanden — neues unten eintragen</span>';
  }
  document.getElementById('modalOverlay').classList.add('show');
  setTimeout(() => document.getElementById('fTitle')?.focus(), 100);
}

async function createTask() {
  const title = document.getElementById('fTitle').value.trim();
  if (!title) { alert('Titel ist Pflicht!'); return; }
  const checks = document.querySelectorAll('#fProjectWrap input[type=checkbox]:checked');
  const fromChecks = Array.from(checks).map(c => c.value);
  const custom = document.getElementById('fProjectNew')?.value.trim() || '';
  const fromCustom = custom ? custom.split(',').map(s => s.trim()).filter(Boolean) : [];
  const projects = [...new Set([...fromChecks, ...fromCustom])];
  await api('/api/tasks', {method:'POST', body:JSON.stringify({
    title,
    body: document.getElementById('fBody').value.trim(),
    procedure: document.getElementById('fProcedure').value.trim(),
    priority: document.getElementById('fPrio').value,
    projects,
    status: document.getElementById('fStatus').value,
    start_date: document.getElementById('fStart').value,
    due_date: document.getElementById('fDue').value,
    estimated_minutes: parseInt(document.getElementById('fEst').value) || 0,
    buffer_percent: parseInt(document.getElementById('fBuf').value) || 20,
  })});
  closeModal();
  await loadTasks();
}

// ── Edit Modal ──
async function editTask(id) {
  const t = allTasks.find(x => x.id === id);
  if (!t) return;
  const prio = PRIO_LABEL(t.priority);

  document.getElementById('modalTitle').textContent = '✏️ Aufgabe bearbeiten';
  document.getElementById('modalBody').innerHTML = `
    <label>Titel</label>
    <input type="text" id="fTitle" value="${escHtml(t.title)}">
    <label>Beschreibung</label>
    <textarea id="fBody">${escHtml(t.body||'')}</textarea>
    <label>📋 Ablauf</label>
    <textarea id="fProcedure" style="min-height:80px;font-family:monospace;font-size:12px">${escHtml(t.procedure||'')}</textarea>
    <div class="row2">
      <div><label>Start</label><input type="date" id="fStart" value="${tsToDateInput(t.started_at)}"></div>
      <div><label>Ziel</label><input type="date" id="fDue" value="${tsToDateInput(t.completed_at)}"></div>
    </div>
    <div class="row3">
      <div><label>⏱ Aufwand</label><input type="number" id="fEst" value="${t.estimated_minutes||0}" min="0" step="5" placeholder="Minuten"></div>
      <div><label>📊 Puffer</label>
        <select id="fBuf">
          ${[0,10,20,30,50,100].map(v => `<option value="${v}" ${(t.buffer_percent||20)==v?'selected':''}>${v}%</option>`).join('')}
        </select>
      </div>
      <div><label>Priorität</label>
        <select id="fPrio">
          <option value="hoch" ${prio==='hoch'?'selected':''}>🔴 Hoch</option>
          <option value="mittel" ${prio==='mittel'?'selected':''}>🟡 Mittel</option>
          <option value="niedrig" ${prio==='niedrig'?'selected':''}>🟢 Niedrig</option>
        </select>
      </div>
    </div>
    <label>📁 Projekte</label>
    <div id="fProjectWrap" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input-bg);color:var(--text);min-height:80px"></div>
    <input type="text" id="fProjectNew" style="width:100%;margin-top:4px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--input-bg);color:var(--text)" placeholder="Neues Projekt (mit Komma trennen)...">
    <div class="row2">
      <div><label>Status</label>
        <select id="fStatus" autocomplete="off">
          ${COLS.includes(t.status) ? '' : `<option value="" selected>⚠ ${escHtml(t.status||'—')}</option>`}
          ${COLS.map(s => `<option value="${s}" ${s===t.status?'selected':''}>${COL_NAMES[s]}</option>`).join('')}
        </select>
      </div>
      <div style="display:flex;align-items:flex-end;gap:8px">
        <button class="btn btn-pomo btn-sm" onclick="startPomoForTask('${t.id}')">🍅 Fokus</button>
      </div>
    </div>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="closeModal()">Abbrechen</button>
      <button class="btn btn-danger" onclick="deleteTask('${t.id}')">🗑 Löschen</button>
      <button class="btn btn-primary" onclick="saveTask('${t.id}')">Speichern</button>
    </div>`;
  document.getElementById('fStatus').addEventListener('change', (e) => {
    if (e.isTrusted) changeStatus(t.id, e.target.value);
  });
  // Populate multi-project checkboxes
  const allProjs = [...new Set(allTasks.flatMap(x => getProjs(x)).filter(Boolean))].sort();
  const tProjs = getProjs(t);
  const pWrap = document.getElementById('fProjectWrap');
  if (pWrap) {
    pWrap.innerHTML = allProjs.map(p =>
      `<label style="display:flex;align-items:center;gap:4px;margin:2px 0;font-size:12px;cursor:pointer">
        <input type="checkbox" value="${escHtml(p)}" ${tProjs.includes(p)?'checked':''}>
        📁 ${escHtml(p)}
      </label>`
    ).join('') || '<span style="color:var(--text-dim);font-size:11px">Keine Projekte vorhanden</span>';
  }
  document.getElementById('modalOverlay').classList.add('show');
  setTimeout(() => document.getElementById('fTitle')?.focus(), 100);
}

async function saveTask(id) {
  const checks = document.querySelectorAll('#fProjectWrap input[type=checkbox]:checked');
  const fromChecks = Array.from(checks).map(c => c.value);
  const custom = document.getElementById('fProjectNew').value.trim() || '';
  const fromCustom = custom ? custom.split(',').map(s => s.trim()).filter(Boolean) : [];
  const projects = [...new Set([...fromChecks, ...fromCustom])];
  await api('/api/tasks/'+id, {method:'PUT', body:JSON.stringify({
    title: document.getElementById('fTitle').value.trim(),
    body: document.getElementById('fBody').value.trim(),
    procedure: document.getElementById('fProcedure').value.trim(),
    priority: document.getElementById('fPrio').value,
    projects,
    start_date: document.getElementById('fStart').value,
    due_date: document.getElementById('fDue').value,
    estimated_minutes: parseInt(document.getElementById('fEst').value) || 0,
    buffer_percent: parseInt(document.getElementById('fBuf').value) || 20,
  })});
  closeModal();
  await loadTasks();
}

function closeModal() {
  document.getElementById('modalOverlay').classList.remove('show');
}

// ── Gantt ──
function renderGantt() {
  const wrap = document.getElementById('ganttWrap');
  const filter = document.getElementById('ganttProjectFilter').value;
  if (!allTasks.length) {
    wrap.innerHTML = '<div class="empty-state">Keine Aufgaben vorhanden</div>';
    return;
  }
  const filtered = filter ? allTasks.filter(t => getProjs(t).includes(filter)) : allTasks;
  const dated = filtered.filter(t => t.started_at || t.completed_at);
  if (!dated.length) {
    wrap.innerHTML = '<div class="empty-state">Keine Aufgaben mit Datum. Setze Start/Ziel in den Karten.</div>';
    return;
  }
  const now = Date.now() / 1000;
  const day = 86400;
  const sorted = [...dated].sort((a,b) => {
    const pa = typeof a.priority === 'number' ? a.priority : ({hoch:1,mittel:2,niedrig:3}[a.priority]||2);
    const pb = typeof b.priority === 'number' ? b.priority : ({hoch:1,mittel:2,niedrig:3}[b.priority]||2);
    if (pa !== pb) return pa - pb;
    return (a.created_at||0) - (b.created_at||0);
  });
  const tasks = sorted.map(t => {
    const prioVal = typeof t.priority === 'number' ? t.priority : ({hoch:1,mittel:2,niedrig:3}[t.priority]||2);
    const start = t.started_at || t.created_at || now;
    let end = t.completed_at || now;
    if (!t.completed_at && t.status !== 'completed') {
      end = now + (prioVal === 1 ? 7*day : prioVal === 2 ? 14*day : 30*day);
    }
    return {...t, _start: start, _end: end};
  });
  const minT = Math.min(...tasks.map(t => t._start));
  const maxT = Math.max(...tasks.map(t => t._end), now + 7*day);
  const range = maxT - minT || day;
  function pct(val) { return Math.max(2, ((val - minT) / range) * 100); }

  let html = '<table class="gantt-table"><thead><tr><th>Projekt</th><th>Prio</th><th>Status</th><th>Aufwand</th><th>📁 Projekt</th><th>Start</th><th>Ziel</th></tr></thead><tbody>';
  tasks.forEach(t => {
    const prio = PRIO_LABEL(t.priority);
    const estStr = t.estimated_minutes > 0 ? fmtMinutes(t.estimated_minutes) + (t.buffer_percent ? ' +'+t.buffer_percent+'%' : '') : '—';
    html += `<tr>
      <td><strong style="${t.status==='completed'?'text-decoration:line-through;color:#999':''}">${escHtml(t.title)}</strong></td>
      <td><span style="color:${PRIO_C[prio]};font-weight:600">${prio}</span></td>
      <td>${COL_NAMES[t.status]||t.status}</td>
      <td style="color:#7C3AED;font-weight:500;font-size:12px">${estStr}</td>
      <td style="font-size:12px">${escHtml(getProjStr(t))}</td>
      <td class="gantt-date">${t.started_at ? tsToDate(t.started_at) : (t.created_at ? tsToDate(t.created_at) : '—')}</td>
      <td class="gantt-date">${t.completed_at ? tsToDate(t.completed_at) : (t.status==='completed' ? '—' : 'offen')}</td>
    </tr>`;
  });
  html += '</tbody></table>';

  html += '<div class="gantt-timeline"><div class="tl-label">📈 Zeitverlauf</div>';
  tasks.forEach(t => {
    const prio = PRIO_LABEL(t.priority);
    const left = pct(t._start);
    const width = Math.max(3, pct(t._end) - left);
    const opacity = t.status === 'completed' ? '0.5' : '1';
    html += `<div class="timeline-row">
      <div class="timeline-label" title="${escHtml(t.title)}" style="${t.status==='completed'?'text-decoration:line-through;color:#999':''}">${escHtml(t.title)}</div>
      <div class="timeline-track">
        <div class="timeline-fill" style="left:${left}%;width:${width}%;background:${PRIO_C[prio]};opacity:${opacity}"></div>
        <div class="timeline-text" style="left:${left}%;width:${width}%">${escHtml(t.title)}</div>
      </div>
    </div>`;
  });
  const todayPct = pct(now);
  html += `<div style="position:relative;height:0;margin-top:-4px">
    <div style="position:absolute;left:${todayPct}%;top:0;width:2px;height:${tasks.length*28+12}px;background:#DD3221;z-index:2"></div>
    <div style="position:absolute;left:${todayPct}%;top:-2px;font-size:10px;color:#DD3221;font-weight:600;transform:translateX(-50%)">▼ Heute</div>
  </div>`;
  html += '</div>';
  wrap.innerHTML = html;

  // --- Routinen Section ---
  const rWrap = document.getElementById('routinesOverview');
  if (rWrap) {
    if (!routines.length) {
      rWrap.innerHTML = '<div style="text-align:center;color:#aaa;padding:20px;font-size:13px">Keine Routinen angelegt</div>';
    } else {
      let rh = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px">';
      routines.forEach(r => {
        const freqLabels = {'daily':'täglich','weekly':'wöchentlich','biweekly':'14-tägig','monthly':'monatlich'};
        const done = r.done_today;
        rh += '<div style="background:var(--surface);border-radius:8px;padding:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;align-items:center;gap:10px;border-left:4px solid '+(done?'#059669':'#e5e7eb')+'">'
          + '<div style="width:28px;height:28px;border-radius:50%;background:'+(done?'#059669':'#e5e7eb')+';color:'+(done?'#fff':'#999')+';display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0">'+(done?'✓':'')+'</div>'
          + '<div style="flex:1;min-width:0">'
          + '<div style="font-size:12px;font-weight:600;color:'+(done?'#999':'#1a1a2e')+'">'+escHtml(r.name)+'</div>'
          + '<div style="font-size:10px;color:#888">'+escHtml(freqLabels[r.freq]||r.freq)+(r.days?' · '+r.days:'')+'</div>'
          + '</div>'
          + (r.last_done ? '<div style="font-size:9px;color:#999;flex-shrink:0">'+new Date(r.last_done*1000).toLocaleDateString('de-DE',{weekday:'short',day:'2-digit'})+'</div>' : '')
          + '</div>';
      });
      rh += '</div>';
      rWrap.innerHTML = '<div style="margin-top:16px"><h3 style="font-size:14px;color:var(--primary);margin-bottom:8px">🔄 Tägliche Routinen</h3>'+rh+'</div>';
    }
  }
}

// ── Pomodoro ──
function togglePomo() {
  const overlay = document.getElementById('pomoOverlay');
  overlay.classList.toggle('show');
  if (!overlay.classList.contains('show')){
    pomoReset();
  }
}

function startPomoForTask(taskId) {
  const t = allTasks.find(x => x.id === taskId);
  if (!t) return;
  document.getElementById('pomoTask').textContent = '🍅 ' + t.title;
  togglePomo();
}

function pomoStart() {
  if (pomoState === 'idle' || pomoState === 'paused' || pomoState === 'break') {
    if (pomoState === 'idle' || pomoState === 'break') {
      pomoRemaining = pomoState === 'break' ? 5 * 60 : 25 * 60;
      pomoState = pomoState === 'break' ? 'break' : 'running';
    } else {
      pomoState = 'running';
    }
    document.getElementById('pomoBtn').textContent = '⏸ Pause';
    document.getElementById('pomoPhase').textContent = pomoState === 'break' ? '☕ Pause' : '🍅 Fokus';
    document.getElementById('pomoTimer').className = 'timer' + (pomoState === 'break' ? ' break' : '');
    if (pomoInterval) clearInterval(pomoInterval);
    pomoInterval = setInterval(pomoTick, 1000);
  } else if (pomoState === 'running') {
    pomoState = 'paused';
    document.getElementById('pomoBtn').textContent = '▶ Weiter';
    document.getElementById('pomoPhase').textContent = '⏸ Pausiert';
    document.getElementById('pomoTimer').className = 'timer pause';
    if (pomoInterval) clearInterval(pomoInterval);
  }
}

function pomoReset() {
  if (pomoInterval) clearInterval(pomoInterval);
  pomoInterval = null;
  pomoState = 'idle';
  pomoRemaining = 25 * 60;
  document.getElementById('pomoTimer').textContent = '25:00';
  document.getElementById('pomoTimer').className = 'timer';
  document.getElementById('pomoBtn').textContent = '▶ Start';
  document.getElementById('pomoPhase').textContent = '🍅 Fokus';
  document.getElementById('pomoTask').textContent = 'Keine Aufgabe ausgewählt';
  updatePomoCount();
}

function pomoTick() {
  pomoRemaining--;
  if (pomoRemaining <= 0) {
    if (pomoState === 'running') {
      // Session complete
      pomoCountToday++;
      localStorage.setItem('pomoCount', pomoCountToday);
      localStorage.setItem('pomoDate', today);
      updatePomoCount();
      if (pomoInterval) clearInterval(pomoInterval);
      pomoState = 'break';
      pomoRemaining = 5 * 60;
      document.getElementById('pomoPhase').textContent = '☕ Pause — gut gemacht!';
      document.getElementById('pomoTimer').className = 'timer break';
      document.getElementById('pomoBtn').textContent = '▶ Pause starten';
      try {
        if (Notification.permission === 'granted') {
          new Notification('🍅 Pomodoro abgeschlossen!', {body: '5 Minuten Pause.'});
        }
      } catch(e) {}
      return;
    } else if (pomoState === 'break') {
      if (pomoInterval) clearInterval(pomoInterval);
      pomoState = 'idle';
      pomoRemaining = 25 * 60;
      document.getElementById('pomoPhase').textContent = '🍅 Bereit für nächste Runde!';
      document.getElementById('pomoTimer').textContent = '25:00';
      document.getElementById('pomoTimer').className = 'timer';
      document.getElementById('pomoBtn').textContent = '▶ Start';
      try {
        if (Notification.permission === 'granted') {
          new Notification('☕ Pause vorbei!', {body: 'Nächster Fokus-Durchgang.'});
        }
      } catch(e) {}
      return;
    }
  }
  const m = Math.floor(pomoRemaining / 60);
  const s = pomoRemaining % 60;
  document.getElementById('pomoTimer').textContent = String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  // Update browser title
  document.title = '🍅 ' + document.getElementById('pomoTimer').textContent + ' · Projekte';
}

function updatePomoCount() {
  document.getElementById('pomoCount').textContent = '🍅 ' + pomoCountToday + ' Today';
}

// Request notification permission on interaction
document.addEventListener('click', () => {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}, {once: true});

// ── Project Overview ──
function renderOverview() {
  const wrap = document.getElementById('overviewWrap');
  if (!allTasks.length) {
    wrap.innerHTML = '<div class="empty-state">Keine Aufgaben vorhanden</div>';
    return;
  }
  const byProj = {};
  allTasks.forEach(t => {
    const pList = getProjs(t);
    const p = pList.length ? pList[0] : '(ohne Projekt)';
    if (!byProj[p]) byProj[p] = [];
    byProj[p].push(t);
  });
  const projNames = Object.keys(byProj).sort();
  const colors = {'backlog':'#e5e7eb','ready':'#f59e0b','running':'#059669','completed':'var(--primary)','blocked':'#DC2626'};
  let totalEst = 0, totalBuf = 0, totalTasks = allTasks.length;
  let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px">';
  projNames.forEach(p => {
    const tasks = byProj[p];
    const count = tasks.length;
    const est = tasks.reduce((s,t) => s + (t.estimated_minutes||0), 0);
    const buf = tasks.reduce((s,t) => s + Math.round((t.estimated_minutes||0) * (t.buffer_percent||20) / 100), 0);
    totalEst += est; totalBuf += buf;
    const byStatus = {};
    tasks.forEach(t => { byStatus[t.status] = (byStatus[t.status]||0) + 1; });
    const starts = tasks.map(t => t.started_at||t.created_at).filter(Boolean).sort();
    const ends = tasks.map(t => t.completed_at).filter(Boolean).sort();
    const first = starts.length ? tsToDate(starts[0]) : '—';
    const last = ends.length ? tsToDate(ends[ends.length-1]) : 'offen';
    const pctDone = tasks.filter(t => t.status==='completed').length / count * 100;
    html += '<div style="background:var(--surface);border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
      + '<h3 style="font-size:15px;color:var(--primary);font-weight:600;cursor:pointer" onclick="filterProject(\''+escHtml(p)+'\')">📁 '+escHtml(p)+'</h3>'
      + '<span style="font-size:12px;color:#888;font-weight:500">'+count+' Aufgaben</span></div>'
      + '<div style="height:6px;background:#e5e7eb;border-radius:3px;margin-bottom:10px;overflow:hidden">'
      + '<div style="height:100%;width:'+Math.round(pctDone)+'%;background:#059669;border-radius:3px;transition:width .5s"></div></div>'
      + '<table style="width:100%;font-size:12px;border-collapse:collapse">'
      + '<tr><td style="padding:2px 4px;color:#666">⏱ Aufwand</td><td style="padding:2px 4px;font-weight:600">'+_fmtM(est)+(buf?' + '+_fmtM(buf)+' Puffer':'')+'</td></tr>'
      + '<tr><td style="padding:2px 4px;color:#666">📅 Start</td><td style="padding:2px 4px">'+first+'</td></tr>'
      + '<tr><td style="padding:2px 4px;color:#666">🎯 Ende</td><td style="padding:2px 4px">'+last+'</td></tr>'
      + '<tr><td style="padding:2px 4px;color:#666">📊 Status</td><td style="padding:2px 4px">'
      + Object.entries(byStatus).map(([s,c]) => '<span style="display:inline-block;background:'+(colors[s]||'#999')+';color:'+(s==='backlog'?'#666':'#fff')+';border-radius:4px;padding:1px 6px;font-size:10px;margin:1px">'+(COL_NAMES_SHORT[s]||s)+' '+c+'</span>').join(' ')
      + '</td></tr></table></div>';
  });
  html += '</div>';

  // --- Charts: Zeitverteilung ---
  const now = new Date();
  const dayMs = 86400000;
  const projColors = ['var(--primary)','#DD3221','#f59e0b','#059669','#7C3AED','#DC2626','#6b7280','var(--accent)','#84cc16','#ec4899'];

  // 1) Pie: Geplante Zeit diese Woche nach Projekt
  const weekStart = new Date(now); weekStart.setDate(now.getDate()-now.getDay()+1); weekStart.setHours(0,0,0,0);
  const weekEnd = new Date(weekStart); weekEnd.setDate(weekStart.getDate()+7);
  const ws = Math.floor(weekStart.getTime()/1000);
  const we = Math.floor(weekEnd.getTime()/1000);
  const weekTasks = allTasks.filter(t => {
    const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
    return s && s < we && e >= ws && (t.estimated_minutes||0) > 0;
  });
  const byProjWeek = {};
  weekTasks.forEach(t => {
    const pList2 = getProjs(t);
    const p = pList2.length ? pList2[0] : 'Unsortiert';
    byProjWeek[p] = (byProjWeek[p]||0) + (t.estimated_minutes||0);
  });
  const wpEntries = Object.entries(byProjWeek).sort((a,b) => b[1]-a[1]);
  const wpTotal = wpEntries.reduce((s,[_,v]) => s+v, 0);
  let pieHtml = '';
  if (wpTotal > 0) {
    let conic = wpEntries.map(([p,v],i) => {
      const pct = v/wpTotal*100;
      const angle = i===0 ? 0 : wpEntries.slice(0,i).reduce((s,[_,x]) => s + x/wpTotal*360, 0);
      return projColors[i%projColors.length]+' '+angle+'deg '+(angle+v/wpTotal*360)+'deg';
    }).join(', ');
    pieHtml = '<div style="display:flex;align-items:center;gap:24px;flex-wrap:wrap">'
      + '<div style="width:120px;height:120px;border-radius:50%;background:conic-gradient('+conic+');flex-shrink:0"></div>'
      + '<div style="font-size:12px">'
      + wpEntries.map(([p,v],i) => '<div style="margin:2px 0"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:'+projColors[i%projColors.length]+';margin-right:6px;vertical-align:middle"></span>'+escHtml(p)+' <strong>'+_fmtM(v)+'</strong></div>').join('')
      + '</div></div>';
  } else {
    pieHtml = '<div style="color:#aaa;font-size:13px;padding:10px">Keine geplanten Aufgaben diese Woche mit Datum</div>';
  }

  // 2) Bar Chart: Wöchentlicher Aufwand letzte 6 Wochen
  let barHtml = '<div style="display:flex;align-items:end;gap:6px;height:120px;padding:10px 0">';
  for (let w = 5; w >= 0; w--) {
    const wStart = new Date(now); wStart.setDate(now.getDate()-now.getDay()+1 - w*7); wStart.setHours(0,0,0,0);
    const wEnd = new Date(wStart); wEnd.setDate(wStart.getDate()+7);
    const wst = Math.floor(wStart.getTime()/1000);
    const wet = Math.floor(wEnd.getTime()/1000);
    const wTasks = allTasks.filter(t => {
      const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
      return s && s < wet && e >= wst && (t.estimated_minutes||0) > 0;
    });
    const wEst = wTasks.reduce((s,t) => s+(t.estimated_minutes||0), 0);
    const maxH = 100;
    const h = Math.min(maxH, wEst/60); // 1h = 1px roughly
    barHtml += '<div style="flex:1;display:flex;flex-direction:column;align-items:center">'
      + '<div style="width:100%;background:var(--primary);border-radius:4px 4px 0 0;height:'+h+'px;min-height:'+(wEst>0?4:0)+'px;transition:height .3s"></div>'
      + '<div style="font-size:9px;color:#666;margin-top:4px;text-align:center">'+wStart.toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'})+'</div>'
      + '<div style="font-size:9px;color:#999">'+_fmtM(wEst)+'</div>'
      + '</div>';
  }
  barHtml += '</div>';

  // Charts section
  html += '<div style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:12px">'
    + '<div style="background:var(--surface);border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">'
    + '<h3 style="font-size:14px;color:var(--primary);margin-bottom:12px">🥧 Geplante Zeit diese Woche</h3>'
    + pieHtml
    + '</div>'
    + '<div style="background:var(--surface);border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">'
    + '<h3 style="font-size:14px;color:var(--primary);margin-bottom:12px">📊 Wöchentlicher Aufwand (6 Wochen)</h3>'
    + barHtml
    + '</div>'
    + '</div>';

  // Totals bar
  const totalDone = allTasks.filter(t => t.status==='completed').length;
  html += '<div style="margin-top:16px;background:var(--surface);border-radius:10px;padding:14px 16px;display:flex;gap:24px;flex-wrap:wrap;font-size:13px">'
    + '<span>📊 <strong>'+totalTasks+'</strong> Aufgaben</span>'
    + '<span>⏱ <strong>'+_fmtM(totalEst)+'</strong> geschätzt <span style="color:#888">(+ '+_fmtM(totalBuf)+' Puffer)</span></span>'
    + '<span>✅ <strong>'+totalDone+'</strong> erledigt <span style="color:#888">('+(totalTasks?Math.round(totalDone/totalTasks*100):0)+'%)</span></span>'
    + '</div>';
  wrap.innerHTML = html;
}
function _fmtM(m) { if (!m||m<=0)return'0min'; if(m>=60)return Math.floor(m/60)+'h '+(m%60?m%60+'min':''); return m+'min'; }

// ── Jump to Kanban with project filter ──
function filterProject(name) {
  document.getElementById('projectFilter').value = name;
  switchTab('kanban');
  renderKanban();
}

// ── Routinen ──
let routines = [];
async function toggleRoutine(id) {
  await api('/api/routines/'+id+'/toggle', {method:'POST'});
  await loadTasks();
}

// ── Calendar View ──
let calOffset = 0;
let calView = 'week';
function calSetView(v) {
  calView = v;
  document.querySelectorAll('.cal-vw').forEach(b => {
    b.style.background = b.dataset.vw === v ? 'var(--primary)' : '#e5e7eb';
    b.style.color = b.dataset.vw === v ? '#fff' : '#666';
  });
  renderCalendar();
}
function calNav(d) {
  if (calView === 'month') calOffset += d;
  else if (calView === 'year') calOffset += d;
  else calOffset += d;
  renderCalendar();
}
function calToday() { calOffset = 0; renderCalendar(); }

function renderCalendar() {
  const wrap = document.getElementById('calendarWrap');
  if (calView === 'week') return renderCalWeek(wrap);
  if (calView === 'day') return renderCalDay(wrap);
  if (calView === 'month') return renderCalMonth(wrap);
  renderCalYear(wrap);
}

function renderCalWeek(wrap) {
  const now = new Date();
  const day = now.getDay();
  const diff = now.getDate() - day + (day === 0 ? -6 : 1) + calOffset * 7;
  const start = new Date(now.setDate(diff)); start.setHours(0,0,0,0);
  const days = Array.from({length:7}, (_,i) => { const d=new Date(start); d.setDate(d.getDate()+i); return d; });
  const fmt = d => d.toLocaleDateString('de-DE', {weekday:'short', day:'2-digit', month:'2-digit'});
  const isToday = d => {const t=new Date(); return d.getFullYear()===t.getFullYear()&&d.getMonth()===t.getMonth()&&d.getDate()===t.getDate();};
  document.getElementById('calLabel').textContent = fmt(days[0]) + ' – ' + fmt(days[6]);
  const dayTs = days.map(d => Math.floor(d.getTime()/1000));
  const nextDayTs = days.map(d => Math.floor(new Date(d.getFullYear(),d.getMonth(),d.getDate()+1).getTime()/1000));
  const byDay = days.map(()=>[]);
  const calFilter = document.getElementById('calProjectFilter').value;
  const calTasks = calFilter ? allTasks.filter(t => getProjs(t).includes(calFilter)) : allTasks;
  const calDated = calTasks.filter(t => t.started_at || t.completed_at);
  calDated.forEach(t => {
    const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
    if (!s) return;
    for (let i=0;i<7;i++) if (s<nextDayTs[i]&&e>=dayTs[i]) byDay[i].push(t);
  });
  const colors = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};
  let h = '<table style="width:100%;border-collapse:collapse;table-layout:fixed;min-width:700px"><thead><tr>';
  days.forEach((d,i) => {
    const t = isToday(d) ? ' style="background:var(--primary);color:#fff;border-radius:6px 6px 0 0"' : '';
    h += '<th'+t+' style="padding:8px 6px;font-size:12px;text-align:center;font-weight:600">'+fmt(d)+'</th>';
  });
  h += '</tr></thead><tbody><tr>';
  days.forEach((d,i) => {
    const t = isToday(d) ? ' style="background:#f0f4ff"' : '';
    h += '<td'+t+' style="vertical-align:top;padding:4px;border:1px solid #e5e7eb;height:120px;width:14.28%">';
    if (byDay[i].length) byDay[i].forEach(t => {
      const p = PRIO_LABEL(t.priority);
      h += '<div style="background:'+(colors[p]||'#999')+';color:#fff;border-radius:4px;padding:2px 4px;margin-bottom:2px;font-size:10px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'+(t.status==='completed'?'opacity:0.5;text-decoration:line-through':'')+'" onclick="editTask(\''+t.id+'\')">'+escHtml(t.title)+'</div>';
    });
    h += '</td>';
  });
  h += '</tr></tbody></table>';
  h += '<div style="margin-top:8px;font-size:11px;color:#888;display:flex;gap:12px;flex-wrap:wrap">';
  Object.entries(colors).forEach(([p,c]) => h += '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:'+c+';margin-right:4px;vertical-align:middle"></span>'+p+'</span>');
  h += '</div>';
  wrap.innerHTML = h;
}

function renderCalDay(wrap) {
  const now = new Date();
  now.setDate(now.getDate() + calOffset);
  const fmt = d => d.toLocaleDateString('de-DE', {weekday:'long', day:'2-digit', month:'long', year:'numeric'});
  document.getElementById('calLabel').textContent = fmt(now);
  const dayStart = Math.floor(new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime()/1000);
  const dayEnd = dayStart + 86400;
  const calFilter = document.getElementById('calProjectFilter').value;
  const calTasks = calFilter ? allTasks.filter(t => getProjs(t).includes(calFilter)) : allTasks;
  const calDated = calTasks.filter(t => t.started_at || t.completed_at);
  const tasks = calDated.filter(t => {
    const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
    return s && s < dayEnd && e >= dayStart;
  });
  const colors = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};
  const isToday = new Date().toDateString() === now.toDateString();
  let h = '<div style="background:'+(isToday?'#f0f4ff':'#fff')+';border-radius:8px;padding:16px;min-height:400px">';
  if (!tasks.length) {
    h += '<div class="empty-state">Keine Aufgaben an diesem Tag</div>';
  } else {
    h += '<div style="display:flex;flex-direction:column;gap:6px">';
    tasks.sort((a,b) => (a.priority||2)-(b.priority||2));
    tasks.forEach(t => {
      const prio = PRIO_LABEL(t.priority);
      const done = t.status === 'completed';
      h += '<div class="cal-day-card" data-id="'+t.id+'" style="border-left:4px solid '+(colors[prio]||'#999')+';background:'+(done?'#f9f9f9':'#fff')+';border-radius:6px;padding:10px 12px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;align-items:center;gap:10px">'
        + '<div style="flex:1;min-width:0">'
        + '<div style="font-size:13px;font-weight:600;color:'+(done?'#999':'#1a1a2e')+';'+(done?'text-decoration:line-through':'')+'">'+escHtml(t.title)+'</div>'
        + '<div style="font-size:10px;color:#888;margin-top:2px;display:flex;gap:8px">'
        + '<span style="color:'+(colors[prio]||'#999')+';font-weight:600">'+prio+'</span>'
        + (getProjs(t).length ? '<span>📁 '+escHtml(getProjStr(t))+'</span>' : '')
        + '<span>'+(COL_NAMES[t.status]||t.status)+'</span>'
        + (t.estimated_minutes ? '<span>🕐 '+_fmtM(t.estimated_minutes)+'</span>' : '')
        + '</div></div>'
        + '</div>';
    });
    h += '</div>';
  }
  h += '</div>'; // close else
  // Routines section
  if (routines.length) {
    h += '<h3 style="font-size:14px;color:var(--primary);margin:16px 0 8px">🔄 Routinen</h3>'
      + '<div style="display:flex;flex-direction:column;gap:6px">';
    routines.forEach(r => {
      // Show routines matching today
      const isTodayRoutine = r.freq === 'daily' || (function() {
        const dayEn = ['Su','Mo','Tu','We','Th','Fr','Sa'];
        const todayEn = dayEn[new Date().getDay()];
        if (r.freq === 'weekly' && r.days && r.days.includes(todayEn)) return true;
        if (r.freq === 'biweekly' && r.days && r.days.includes(todayEn)) {
          // Every other week: use even/odd week check
          const weekNum = Math.floor((new Date() - new Date(new Date().getFullYear(),0,1)) / 604800000);
          return weekNum % 2 === 0;
        }
        if (r.freq === 'monthly' && r.days) {
          const dayNum = parseInt(r.days);
          return new Date().getDate() === dayNum;
        }
        return false;
      })();
      if (!isTodayRoutine) return;
      h += '<div class="cal-day-routine" data-rid="'+r.id+'" style="border-left:4px solid '+(r.done_today?'#059669':'#e5e7eb')+';background:var(--surface);border-radius:6px;padding:10px 12px;cursor:pointer;display:flex;align-items:center;gap:10px;box-shadow:0 1px 2px rgba(0,0,0,.06)">'
        + '<div style="width:24px;height:24px;border-radius:50%;background:'+(r.done_today?'#059669':'#e5e7eb')+';color:'+(r.done_today?'#fff':'#999')+';display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0">'+(r.done_today?'✓':'')+'</div>'
        + '<div style="font-size:12px;color:'+(r.done_today?'#999':'#1a1a2e')+';'+(r.done_today?'text-decoration:line-through':'')+'">'+escHtml(r.name)+'</div>'
        + '</div>';
    });
    h += '</div>';
  }
  h += '</div>'; // close day container
  wrap.innerHTML = h;
  // Attach click handlers
  wrap.querySelectorAll('.cal-day-card').forEach(el => {
    el.addEventListener('click', function() { editTask(this.dataset.id); });
  });
  wrap.querySelectorAll('.cal-day-routine').forEach(el => {
    el.addEventListener('click', function() { toggleRoutine(parseInt(this.dataset.rid)); });
  });
}

function renderCalMonth(wrap) {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth() + calOffset;
  const first = new Date(year, month, 1);
  const last = new Date(year, month + 1, 0);
  const startDay = first.getDay(); // 0=Sun
  const daysInMonth = last.getDate();
  const monthLabel = first.toLocaleDateString('de-DE', {month:'long', year:'numeric'});
  document.getElementById('calLabel').textContent = monthLabel;
  const dayLabels = ['Mo','Di','Mi','Do','Fr','Sa','So'];
  let h = '<table style="width:100%;border-collapse:collapse;table-layout:fixed"><thead><tr>';
  dayLabels.forEach(d => h += '<th style="padding:6px;font-size:11px;text-align:center;color:#666">'+d+'</th>');
  h += '</tr></thead><tbody>';
  const colors = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};
  const calFilter = document.getElementById('calProjectFilter').value;
  const calTasks = calFilter ? allTasks.filter(t => getProjs(t).includes(calFilter)) : allTasks;
  const calDated = calTasks.filter(t => t.started_at || t.completed_at);
  const tNow = Math.floor(Date.now()/1000);
  let day = 1;
  for (let row = 0; row < 6; row++) {
    if (day > daysInMonth) break;
    h += '<tr>';
    for (let col = 0; col < 7; col++) {
      const cellDay = row === 0 && col < ((startDay+6)%7) ? 0 : day++;
      const d = cellDay ? new Date(year, month, cellDay) : null;
      const isT = d && d.getFullYear()===now.getFullYear()&&d.getMonth()===now.getMonth()&&d.getDate()===now.getDate();
      h += '<td style="vertical-align:top;padding:2px;border:1px solid #e5e7eb;height:80px;width:14.28%;'+(isT?'background:#f0f4ff':'')+'">';
      if (d) {
        h += '<div style="font-size:10px;font-weight:600;color:'+(isT?'var(--primary)':'#333')+';padding:1px 2px;margin-bottom:2px">'+cellDay+'</div>';
        const dayStart = Math.floor(d.getTime()/1000);
        const dayEnd = dayStart + 86400;
        const tasks = calDated.filter(t => {
          const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
          return s && s < dayEnd && e >= dayStart;
        }).slice(0, 3);
        tasks.forEach(t => {
          const p = PRIO_LABEL(t.priority);
          h += '<div style="background:'+(colors[p]||'#999')+';color:#fff;border-radius:2px;padding:1px 2px;margin-bottom:1px;font-size:8px;cursor:pointer;overflow:hidden;white-space:nowrap;'+(t.status==='completed'?'opacity:0.5;text-decoration:line-through':'')+'" onclick="editTask(\''+t.id+'\')">'+escHtml(t.title).substring(0,15)+'</div>';
        });
        if (tasks.length > 3) h += '<div style="font-size:8px;color:#999">+'+ (calDated.filter(t => {
          const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
          return s && s < dayEnd && e >= dayStart;
        }).length - 3) +' mehr</div>';
      }
      h += '</td>';
    }
    h += '</tr>';
  }
  h += '</tbody></table>';
  wrap.innerHTML = h;
}

function renderCalYear(wrap) {
  const now = new Date();
  const year = now.getFullYear() + calOffset;
  document.getElementById('calLabel').textContent = 'Jahr ' + year;
  const colors = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};
  const calFilter = document.getElementById('calProjectFilter').value;
  const calTasks = calFilter ? allTasks.filter(t => getProjs(t).includes(calFilter)) : allTasks;
  let h = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">';
  for (let m = 0; m < 12; m++) {
    const first = new Date(year, m, 1);
    const lastD = new Date(year, m + 1, 0).getDate();
    const mName = first.toLocaleDateString('de-DE', {month:'short'});
    const monthStart = Math.floor(first.getTime()/1000);
    const monthEnd = monthStart + lastD * 86400;
    const tasks = calDated.filter(t => {
      const s = t.started_at||t.created_at; const e = t.completed_at||s+86400*14;
      return s && s < monthEnd && e >= monthStart;
    });
    const byPrio = {hoch:0,mittel:0,niedrig:0};
    tasks.forEach(t => { const p=PRIO_LABEL(t.priority); byPrio[p]=(byPrio[p]||0)+1; });
    h += '<div style="background:var(--surface);border-radius:8px;padding:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)">'
      + '<div style="font-size:13px;font-weight:600;color:var(--primary);margin-bottom:6px">'+mName+'</div>'
      + '<div style="font-size:11px;color:#666">'+tasks.length+' Aufgaben</div>';
    Object.entries(byPrio).forEach(([p,c]) => {
      if (c) h += '<div style="font-size:10px;color:'+(colors[p]||'#999')+'">'+p+': '+c+'</div>';
    });
    h += '</div>';
  }
  h += '</div>';
  wrap.innerHTML = h;
}

// ── Tabs ──
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const tab = document.querySelector(`.tab[onclick*="'${name}'"]`);
  if (tab) tab.classList.add('active');
  const panel = document.getElementById('panel-' + name);
  if (panel) panel.classList.add('active');
  if (name === 'gantt') renderGantt();
  if (name === 'overview') renderOverview();
  if (name === 'calendar') renderCalendar();
}

// Restore title when timer done
setInterval(() => {
  if (pomoState === 'idle' || pomoState === 'paused') {
    document.title = 'Projekte · Paul Dubrownik';
  }
}, 5000);

// ── Dark Mode Toggle ──
function toggleTheme(){
  const isDark=document.body.classList.toggle('dark-mode');
  localStorage.setItem('kanbanTheme',isDark?'dark':'light');
  document.getElementById('themeFab').textContent=isDark?'☀️':'🌙';
}
(function(){
  if(localStorage.getItem('kanbanTheme')==='dark'){
    document.body.classList.add('dark-mode');
    document.getElementById('themeFab').textContent='☀️';
  }
})();

// ── KI-Smart-Import ──
const KI_PROJEKTE = [
  { keys: ['rechnung','konto','abo','versicherung','bank','schulden','geld','überweisen','klarna','adac','revolut','kündigen','paypal','steuer'], name:'🔴 Finanzen & Admin' },
  { keys: ['chef','gehalt','job','schule','bewerbung','website','arbeitgeber','arbeitnehmer','stunden','stundenzettel','claude','mercat','danju','kurs','fortbildung','rettungsschwimmer','erste hilfe'], name:'💼 Beruf & Schule' },
  { keys: ['zimmer','tür','hecke','pflanze','topf','garten','reparier','umbau','streichen','klinke','gewächshaus','palme','vorhang','nähen','föhn','homescreen','youtube','setup','scherbe','kasten','nebelmaschine','flurpflanze'], name:'🛠️ Zuhause & Handwerk' },
  { keys: ['fitness','training','sport','schwimmen','laufen','ernährung','arzt','friseur','puls','schuhe','badekappe','neopren','trinkflasche','trichterbrust','sprungkraft','journal','zahnschutz','vitamin','protein'], name:'🏋️ Körper & Fitness' },
  { keys: ['kaufen','bestellen','einkaufen','supermarkt','rossmann','mediamarkt','decathlon','lidl','rewe','netto','shampoo','sonnencreme','rasierer','mehrfachstecker','thermostat','apple kabel','schuhkleber','sonnenbrille','adidas','vinted'], name:'🛍️ Besorgungen & Shopping' },
  { keys: ['mama','papa','opa','oma','freund','familie','geburtstag','anruf','geschenk','kartfahren','schach','lotte','peer','shishabar','felix','denise','dkms','galaxy buds'], name:'🤝 Soziales & Familie' },
  { keys: ['film','buch','musik','lernen','serie','kino','duolingo','nolan','michael jackson','hail mary','life maxing','dehnen','atmen','twingo','boot','roller'], name:'🎬 Freizeit & Medien' },
  { keys: ['secondhand','flohmarkt','vinted'], name:'🛍️ Vinted & Secondhand' },
];

function kiErkenneProjekt(text) {
  const t = text.toLowerCase();
  for (const p of KI_PROJEKTE) {
    if (p.keys.some(k => t.includes(k))) return p.name;
  }
  return '';
}
function kiErkennePrio(text) {
  const t = text.toLowerCase();
  if (/\bdringend\b|\bsofort\b|\basap\b|\bwichtig\b|!!/.test(t)) return 'hoch';
  if (/\birgendwann\b|\bvielleicht\b|\boptional\b/.test(t)) return 'niedrig';
  return 'mittel';
}
function kiErkenneDauer(text) {
  const m = text.match(/(?:ca\.?\s*)?(\d+)\s*(?:min|minuten|m\b)/i) || text.match(/(?:ca\.?\s*)?(\d+)\s*(?:h|stunden|hour)/i);
  if (m) {
    const n = parseInt(m[1]);
    if (m[0].toLowerCase().includes('h') || m[0].toLowerCase().includes('stunden') || m[0].toLowerCase().includes('hour')) return n * 60;
    return n;
  }
  return 0;
}
function kiErkenneStatus(text) {
  if (text.includes('[blocked]') || /warte auf|blockiert/i.test(text)) return 'blocked';
  if (text.startsWith('!!') || /dringend|sofort/i.test(text)) return 'ready';
  return 'backlog';
}
function kiParseTitle(text) {
  let t = text;
  // remove markers
  t = t.replace(/^!!\s*/, '');
  t = t.replace(/^\[blocked\]\s*/i, '');
  // remove duration like (30min), (2h)
  t = t.replace(/\s*\(?\s*(?:ca\.?\s*)?\d+\s*(?:min|minuten|m\b|h|stunden|hour)s?\)?\s*/gi, '');
  // remove priority markers
  t = t.replace(/^Dringend:\s*/i, '').replace(/^Wichtig:\s*/i, '');
  return t.trim();
}
function kiParseTasks(text) {
  const lines = text.split('\n');
  let currentProject = '';
  const tasks = [];
  for (let line of lines) {
    const t = line.trim();
    if (!t) continue;
    if (t.startsWith('###')) {
      currentProject = t.replace(/^###\s*/, '').trim();
      continue;
    }
    if (t.startsWith('*') || t.startsWith('-')) {
      let raw = t.replace(/^[\*\-]\s*/, '').trim();
      const aiProj = kiErkenneProjekt(raw);
      const project = currentProject || aiProj;
      tasks.push({
        title: kiParseTitle(raw),
        raw: raw,
        project: project,
        priority: kiErkennePrio(raw),
        estimated: kiErkenneDauer(raw),
        status: kiErkenneStatus(raw),
      });
    }
  }
  return tasks;
}
function openImportModal(){
  document.getElementById('importText').value = '';
  document.getElementById('importProgress').style.display = 'none';
  document.getElementById('importPreview').style.display = 'none';
  document.getElementById('importOverlay').classList.add('show');
}
function closeImportModal(){
  document.getElementById('importOverlay').classList.remove('show');
}
function previewImport(){
  const text = document.getElementById('importText').value;
  if (!text.trim()) return;
  const tasks = kiParseTasks(text);
  if (!tasks.length) { alert('Keine Aufgaben gefunden.'); return; }
  const prev = document.getElementById('importPreview');
  prev.style.display = 'block';
  prev.innerHTML = '<div style="font-weight:700;margin-bottom:6px">🔍 KI-Analyse — ' + tasks.length + ' Aufgaben erkannt:</div>' +
    tasks.map((t,i) => {
      const emoji = t.status==='blocked'?'🔴':t.status==='ready'?'🟢':'📋';
      const prioIcon = t.priority==='hoch'?'🔴':t.priority==='niedrig'?'🟢':'🟡';
      const dauer = t.estimated ? ' ⏱'+t.estimated+'min' : '';
      return `<div style="padding:4px 0;border-bottom:1px solid var(--border)">${i+1}. ${emoji} <b>${escHtml(t.title)}</b> ${prioIcon} ${t.project ? '<span style="color:var(--text-dim)">📁'+escHtml(t.project)+'</span>':''}${dauer}</div>`;
    }).join('');
}
async function runImport(){
  const text = document.getElementById('importText').value;
  if (!text.trim()) return;
  const tasks = kiParseTasks(text);
  if (!tasks.length) { alert('Keine Aufgaben gefunden.'); return; }

  const progress = document.getElementById('importProgress');
  progress.style.display = 'block';
  progress.innerHTML = `🧠 ${tasks.length} Aufgaben werden analysiert & erstellt …`;

  let created = 0;
  for (const t of tasks) {
    try {
      await api('/api/tasks', {method:'POST', body:JSON.stringify({
        title: t.title,
        status: t.status,
        projects: t.project ? [t.project] : [],
        priority: t.priority,
        body: '',
        procedure: '',
        start_date: '',
        due_date: '',
        estimated_minutes: t.estimated,
        buffer_percent: 20,
      })});
      created++;
      progress.innerHTML = `✅ ${created} / ${tasks.length} erstellt …`;
    } catch(e) {
      progress.innerHTML += `<br>❌ ${escHtml(t.title)}: ${e.message}`;
    }
  }
  progress.innerHTML = `🎉 ${created} von ${tasks.length} KI-optimierten Aufgaben erstellt! Seite lädt neu …`;
  await loadTasks();
  closeImportModal();
}

// ── Smart Quick-Add ──
async function smartAdd(text){
  if (!text.trim()) return;
  const inp = document.getElementById('smartInput');
  inp.disabled = true;
  const prev = inp.value;
  inp.value = '🧠 analysiere …';
  const tasks = kiParseTasks('* ' + text);
  if (!tasks.length) { inp.value = prev; inp.disabled = false; return; }
  const t = tasks[0];
  try {
    await api('/api/tasks', {method:'POST', body:JSON.stringify({
      title: t.title,
      status: t.status,
      projects: t.project ? [t.project] : [],
      priority: t.priority,
      body: '',
      procedure: '',
      start_date: '',
      due_date: '',
      estimated_minutes: t.estimated,
      buffer_percent: 20,
    })});
    inp.value = '';
    await loadTasks();
  } catch(e) {
    inp.value = '❌ ' + e.message;
    setTimeout(() => { inp.value = prev; }, 2000);
  }
  inp.disabled = false;
}

// ── Init ──
loadTasks();
</script>
</body>
</html>
"""

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