#!/usr/bin/env python3
"""Interaktives Kanban + Gantt + Brainstorming + Pomodoro — API-Server + Frontend.

Start:  python3 server.py
Dann:   http://localhost:8089
"""

import json
import sqlite3
import uuid
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

HOME = Path.home()
KANBAN_DB = HOME / ".hermes" / "kanban" / "boards" / "christian" / "kanban.db"
PORT = 8089
HOST = "127.0.0.1"

PRIO_LABELS = {1: "hoch", 2: "mittel", 3: "niedrig"}
PRIO_VALUES = {"hoch": 1, "mittel": 2, "niedrig": 3}
STATUS_ORDER = ["backlog", "ready", "running", "completed", "blocked"]


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
    con.commit()
    con.close()


ensure_tables()


def fetch_tasks():
    con = get_db()
    rows = con.execute(
        "SELECT id, title, body, status, priority, created_at, started_at, "
        "completed_at, assignee, project_id FROM tasks ORDER BY priority, created_at"
    ).fetchall()
    tasks = [dict(r) for r in rows]
    # Add estimates
    est_rows = con.execute("SELECT * FROM task_estimates").fetchall()
    estimates = {r["task_id"]: dict(r) for r in est_rows}
    con.close()
    for t in tasks:
        e = estimates.get(t["id"], {})
        t["estimated_minutes"] = e.get("estimated_minutes", 0)
        t["buffer_percent"] = e.get("buffer_percent", 20)
    return tasks


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

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/":
            self.send_html(INDEX_HTML)
        elif path == "/api/tasks":
            self.send_json({"tasks": fetch_tasks()})
        elif path == "/api/brainstorm":
            self.send_json({"entries": fetch_brainstorms()})
        elif path.startswith("/api/tasks/"):
            task_id = path.split("/api/tasks/")[1].split("/")[0]
            if task_id:
                con = get_db()
                row = con.execute(
                    "SELECT id, title, body, status, priority, created_at, started_at, "
                    "completed_at, assignee, project_id FROM tasks WHERE id=?", (task_id,)
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
            project = body.get("project", "").strip()
            con = get_db()
            con.execute(
                "INSERT INTO tasks (id, title, body, status, priority, created_at, assignee, started_at, completed_at, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, desc, status, prio, now, "user", start_ts or None, due_ts or None, project or None),
            )
            con.execute(
                "INSERT OR REPLACE INTO task_estimates (task_id, estimated_minutes, buffer_percent) VALUES (?, ?, ?)",
                (task_id, est_min, buf_pct),
            )
            con.commit()
            con.close()
            self.send_json({"ok": True, "id": task_id})

        elif path == "/api/brainstorm":
            content = body.get("content", "").strip()
            project = body.get("project", "").strip()
            if not content:
                self.send_json({"error": "empty content"}, 400)
                return
            now = int(time.time())
            con = get_db()
            con.execute("INSERT INTO brainstorm (content, project, processed, created_at) VALUES (?, ?, 0, ?)",
                        (content, project, now))
            con.commit()
            con.close()
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
            project = body.get("project", "")
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
            if body.get("project") is not None:
                updates.append("project_id=?")
                params.append(project.strip() or None)

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

            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
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
            self.send_json({"ok": True})
        elif path.startswith("/api/brainstorm/"):
            entry_id = path.split("/api/brainstorm/")[1].split("/")[0]
            con = get_db()
            con.execute("DELETE FROM brainstorm WHERE id=?", (entry_id,))
            con.commit()
            con.close()
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


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Projekte · Christian Radden</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f6f8;color:#1a1a2e;padding:20px}
h1{font-size:22px;color:#002D69;margin-bottom:4px}
h1 span{color:#007FA7;font-weight:400}
.sub{font-size:13px;color:#888;margin-bottom:16px}
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:2px solid #e5e7eb}
.tab{padding:10px 20px;cursor:pointer;font-size:14px;font-weight:600;background:transparent;color:#888;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;transition:.2s}
.tab:hover{color:#002D69}
.tab.active{color:#002D69;border-bottom-color:#002D69}
.panel{display:none}
.panel.active{display:block}
.toolbar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.btn{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:.2s;display:inline-flex;align-items:center;gap:4px;font-family:inherit}
.btn-primary{background:#002D69;color:#fff}
.btn-primary:hover{background:#003d8f}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-danger{background:#DC2626;color:#fff}
.btn-danger:hover{background:#B91C1C}
.btn-ghost{background:transparent;color:#666}
.btn-ghost:hover{background:#e5e7eb;color:#333}
.btn-success{background:#059669;color:#fff}
.btn-success:hover{background:#047857}
.btn-pomo{background:#7C3AED;color:#fff}
.btn-pomo:hover{background:#6D28D9}

/* Kanban */
.kanban{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;min-height:60vh}
.column{background:#e5e7eb;border-radius:10px;padding:10px;min-height:200px;display:flex;flex-direction:column}
.col-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:2px solid #ddd;font-size:12px;color:#666}
.col-header h3{font-size:13px;font-weight:600}
.col-count{background:#002D69;color:#fff;font-size:10px;border-radius:10px;padding:1px 7px;font-weight:600}
.col-body{flex:1;min-height:100px}
.card{background:#fff;border-radius:8px;padding:12px;margin-bottom:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);cursor:pointer;transition:.2s;border-left:4px solid #999;position:relative}
.card:hover{box-shadow:0 2px 8px rgba(0,0,0,.12)}
.card.prio-hoch{border-left-color:#DD3221}
.card.prio-mittel{border-left-color:#f59e0b}
.card.prio-niedrig{border-left-color:#6b7280}
.card-title{font-size:13px;font-weight:600;color:#1a1a2e;margin-bottom:2px;padding-right:50px}
.card-body{font-size:11px;color:#666;line-height:1.3;margin-top:4px}
.card-meta{font-size:10px;color:#999;margin-top:6px;display:flex;gap:8px;flex-wrap:wrap}
.card-dates{font-size:9px;color:#aaa;margin-top:4px;display:flex;gap:6px}
.card-est{font-size:9px;color:#7C3AED;margin-top:2px;font-weight:500}
.card-actions{position:absolute;top:6px;right:6px;display:flex;gap:2px}
.card-actions button{background:none;border:none;cursor:pointer;font-size:14px;color:#999;padding:2px 4px;border-radius:4px;transition:.2s}
.card-actions button:hover{background:#f0f0f0;color:#333}
.status-select{font-size:10px;padding:2px 4px;border:1px solid #ddd;border-radius:4px;background:#fff;cursor:pointer;color:#555}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:1000;justify-content:center;align-items:center;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:#fff;border-radius:12px;padding:24px;max-width:600px;width:100%;max-height:85vh;overflow-y:auto;box-shadow:0 4px 24px rgba(0,0,0,.2);animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.modal-close{float:right;font-size:22px;cursor:pointer;color:#999;line-height:1}
.modal-close:hover{color:#333}
.modal h2{color:#002D69;font-size:18px;margin-bottom:16px;padding-right:30px}
.modal label{display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px;margin-top:12px}
.modal input[type=text],.modal input[type=date],.modal input[type=number],.modal textarea,.modal select{width:100%;padding:8px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;font-family:inherit}
.modal textarea{min-height:80px;resize:vertical}
.modal .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.modal .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.modal .btn-row{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}

/* Gantt */
.gantt{overflow-x:auto;background:#fff;border-radius:10px;padding:16px}
.gantt-table{width:100%;border-collapse:collapse;font-size:13px;min-width:750px}
.gantt-table th{background:#002D69;color:#fff;padding:8px 12px;text-align:left;font-size:12px;position:sticky;top:0}
.gantt-table td{padding:8px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
.gantt-date{font-size:10px;color:#999;white-space:nowrap}
.gantt-timeline{position:relative;margin-top:16px;padding-top:12px;border-top:1px solid #e5e7eb}
.gantt-timeline .tl-label{font-size:11px;font-weight:600;color:#666;margin-bottom:6px}
.timeline-row{display:flex;align-items:center;margin-bottom:4px;position:relative}
.timeline-label{width:140px;font-size:12px;font-weight:500;color:#333;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:8px;flex-shrink:0}
.timeline-track{flex:1;height:24px;background:#f0f0f0;border-radius:4px;position:relative;overflow:hidden}
.timeline-fill{position:absolute;top:0;left:0;height:100%;border-radius:4px;min-width:4px;transition:width .5s}
.timeline-text{position:absolute;top:0;left:4px;height:100%;display:flex;align-items:center;font-size:10px;color:#fff;font-weight:600;white-space:nowrap}

/* Brainstorming */
.brainstorm-panel{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.brainstorm-input{background:#fff;border-radius:10px;padding:16px}
.brainstorm-input textarea{width:100%;min-height:150px;padding:10px;border:1px solid #ddd;border-radius:6px;font-size:14px;font-family:inherit;resize:vertical}
.brainstorm-input .meta-row{display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap}
.brainstorm-input .meta-row input{flex:1;min-width:120px;padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px}
.brainstorm-list{background:#fff;border-radius:10px;padding:16px;max-height:70vh;overflow-y:auto}
.bstorm-entry{padding:10px 0;border-bottom:1px solid #f0f0f0}
.bstorm-entry:last-child{border-bottom:none}
.bstorm-content{font-size:13px;color:#333;margin-bottom:4px;white-space:pre-wrap}
.bstorm-meta{font-size:10px;color:#999;display:flex;gap:8px;align-items:center}
.bstorm-project{display:inline-block;background:#e5e7eb;border-radius:4px;padding:1px 6px;font-size:10px;color:#555}
.bstorm-actions{display:flex;gap:4px;margin-top:4px}
.bstorm-actions button{font-size:10px;padding:2px 8px}

/* Pomodoro */
.pomo-fab{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#7C3AED;color:#fff;border:none;cursor:pointer;font-size:24px;box-shadow:0 4px 16px rgba(124,58,237,.4);z-index:900;transition:.2s}
.pomo-fab:hover{transform:scale(1.1);background:#6D28D9}
.pomo-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.85);z-index:9998;justify-content:center;align-items:center;flex-direction:column}
.pomo-overlay.show{display:flex}
.pomo-card{background:#fff;border-radius:20px;padding:40px;text-align:center;max-width:360px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.3)}
.pomo-card .phase{font-size:14px;font-weight:600;color:#7C3AED;margin-bottom:8px}
.pomo-card .timer{font-size:72px;font-weight:700;color:#1a1a2e;font-variant-numeric:tabular-nums;margin:16px 0}
.pomo-card .timer.pause{color:#f59e0b}
.pomo-card .timer.break{color:#059669}
.pomo-card .pomo-btn-row{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.pomo-card .pomo-btn-row button{min-width:80px}
.pomo-card .task-label{font-size:12px;color:#888;margin-top:12px;padding:8px;background:#f4f6f8;border-radius:8px}
.pomo-card .close-pomo{position:absolute;top:12px;right:12px;font-size:24px;cursor:pointer;color:#999;background:none;border:none}
.pomo-card-wrap{position:relative}
.pomo-count{font-size:11px;color:#aaa;margin-top:8px}

/* Misc */
.empty-state{padding:40px 20px;text-align:center;color:#aaa;font-size:14px}
.loading{text-align:center;padding:40px;color:#888;font-size:14px}
.spinner{display:inline-block;width:20px;height:20px;border:3px solid #e5e7eb;border-top-color:#002D69;border-radius:50%;animation:spin .6s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:1000px){.kanban{grid-template-columns:repeat(3,1fr)}.brainstorm-panel{grid-template-columns:1fr}}
@media(max-width:700px){.kanban{grid-template-columns:repeat(2,1fr)}.timeline-label{width:100px;font-size:11px}}
@media(max-width:500px){.kanban{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>Projekte <span>· Christian Radden</span></h1>
<div class="sub" id="statusLine">Lade Daten …</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('kanban')">📋 Kanban</button>
  <button class="tab" onclick="switchTab('gantt')">📊 Gantt</button>
  <button class="tab" onclick="switchTab('brainstorm')">💡 Brainstorming</button>
  <button class="tab" onclick="switchTab('overview')">📊 Übersicht</button>
  <button class="tab" onclick="switchTab('calendar')">📅 Kalender</button>
</div>

<div id="panel-kanban" class="panel active">
  <div class="toolbar">
    <button class="btn btn-primary" onclick="openCreateModal()">+ Neue Karte</button>
    <select id="projectFilter" onchange="renderKanban()" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;background:#fff">
      <option value="">📁 Alle Projekte</option>
    </select>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="kanban" class="kanban"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<div id="panel-gantt" class="panel">
  <div class="toolbar">
    <select id="ganttProjectFilter" onchange="renderGantt()" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;background:#fff">
      <option value="">📁 Alle Projekte</option>
    </select>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="ganttWrap" class="gantt"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<div id="panel-brainstorm" class="panel">
  <div class="brainstorm-panel">
    <div class="brainstorm-input">
      <label style="font-weight:600;font-size:14px;color:#002D69">💡 Gedanken reinschmeißen</label>
      <textarea id="bsInput" placeholder="Was beschäftigt dich? Ideen, To-dos, Gedankenfetzen …"></textarea>
      <div class="meta-row">
        <input id="bsProject" placeholder="Projekt (optional, z.B. Jessi, NGD, EnnAIgram)" list="projectList">
        <datalist id="projectList"></datalist>
        <button class="btn btn-success btn-sm" onclick="submitBrainstorm()">Abschicken</button>
      </div>
      <div style="margin-top:8px;font-size:11px;color:#888">
        <em>Ich mach daraus Kanban-Karten – sag einfach Bescheid wenn du bereit bist.</em>
      </div>
    </div>
    <div class="brainstorm-list" id="bsList">
      <div class="loading"><span class="spinner"></span>Lade …</div>
    </div>
  </div>
</div>

<div id="panel-overview" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="overviewWrap"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<div id="panel-calendar" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="prevWeek()">◀</button>
    <span id="calWeekLabel" style="font-size:14px;font-weight:600;color:#002D69;min-width:200px;text-align:center"></span>
    <button class="btn btn-ghost" onclick="nextWeek()">▶</button>
    <button class="btn btn-ghost" onclick="todayWeek()">Heute</button>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄</button>
  </div>
  <div id="calendarWrap" style="background:#fff;border-radius:10px;padding:16px;overflow-x:auto">
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

<!-- Pomodoro FAB -->
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
let allBs = [];

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
    const [td, bd] = await Promise.all([
      api('/api/tasks'),
      api('/api/brainstorm')
    ]);
    allTasks = td.tasks || [];
    allBs = bd.entries || [];
    document.getElementById('statusLine').textContent =
      allTasks.length + ' Aufgaben · ' + allBs.length + ' Brainstorms · ' + new Date().toLocaleTimeString('de-DE');

    // Update project filters
    const projs = [...new Set(allTasks.map(t => t.project_id).filter(Boolean))].sort();
    const opts = projs.map(p => `<option value="${escHtml(p)}">${escHtml(p)}</option>`).join('');
    const filterSel = document.getElementById('projectFilter');
    const currentVal = filterSel.value;
    filterSel.innerHTML = '<option value="">📁 Alle Projekte</option>' + opts;
    filterSel.value = currentVal;
    const ganttSel = document.getElementById('ganttProjectFilter');
    const ganttVal = ganttSel.value;
    ganttSel.innerHTML = '<option value="">📁 Alle Projekte</option>' + opts;
    ganttSel.value = ganttVal;

    renderKanban();
    renderGantt();
    renderBrainstorm();
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
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
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

function renderKanban() {
  const filter = document.getElementById('projectFilter').value;
  const kanban = document.getElementById('kanban');
  const filtered = filter ? allTasks.filter(t => (t.project_id||'') === filter) : allTasks;
  kanban.innerHTML = COLS.map(col => {
    const items = filtered.filter(t => t.status === col);
    return `<div class="column">
      <div class="col-header">
        <h3>${COL_NAMES[col]}</h3>
        <span class="col-count">${items.length}</span>
      </div>
      <div class="col-body" data-col="${col}">
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
  const projHtml = t.project_id ? `<span style="background:#e5e7eb;border-radius:3px;padding:1px 5px;font-size:9px;color:#555">📁 ${escHtml(t.project_id)}</span>` : '';
  return `<div class="card prio-${prio}" onclick="editTask('${t.id}')">
    <div class="card-actions" onclick="event.stopPropagation()">
      <button onclick="event.stopPropagation();startPomoForTask('${t.id}')" title="Fokus">🍅</button>
      <button onclick="deleteTask('${t.id}')" title="Löschen">🗑</button>
    </div>
    <div class="card-title">${escHtml(t.title)}</div>
    ${bodyShort ? `<div class="card-body">${escHtml(bodyShort)}</div>` : ''}
    ${estHtml}
    ${datesHtml}
    <div class="card-meta">
      <span style="color:${PRIO_C[prio]||'#999'};font-weight:600">${prio}</span>
      ${projHtml}
      <select class="status-select" onchange="changeStatus('${t.id}',this.value)" onclick="event.stopPropagation()">
        ${COLS.map(s => `<option value="${s}" ${s===t.status?'selected':''}>${COL_NAMES_SHORT[s]||s}</option>`).join('')}
      </select>
    </div>
  </div>`;
}

// ── Status + Delete ──
async function changeStatus(id, newStatus) {
  await api('/api/tasks/'+id+'/status', {method:'POST', body:JSON.stringify({status:newStatus})});
  await loadTasks();
}
async function deleteTask(id) {
  if (!confirm('Aufgabe löschen?')) return;
  await api('/api/tasks/'+id, {method:'DELETE'});
  await loadTasks();
}

// ── Create Modal ──
function openCreateModal() {
  document.getElementById('modalTitle').textContent = 'Neue Aufgabe';
  document.getElementById('modalBody').innerHTML = `
    <label>Titel *</label>
    <input type="text" id="fTitle" placeholder="Aufgabe …" autofocus>
    <label>Beschreibung</label>
    <textarea id="fBody" placeholder="Details …"></textarea>
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
    <label>📁 Projekt</label>
    <select id="fProject" onchange="if(this.value==='__new__'){this.style.display='none';document.getElementById('fProjectNew').style.display='block';document.getElementById('fProjectNew').focus()}">
      <option value="">— Keins —</option>
      <option value="__new__">➕ Neu …</option>
    </select>
    <input type="text" id="fProjectNew" style="display:none" placeholder="Neues Projekt …">
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
  // Populate project select
  const projs = [...new Set(allTasks.map(t => t.project_id).filter(Boolean))].sort();
  const projSel = document.getElementById('fProject');
  if (projSel) {
    const cur = projSel.value;
    projSel.innerHTML = '<option value="">— Keins —</option>'
      + projs.map(p => '<option value="'+escHtml(p)+'">'+escHtml(p)+'</option>').join('')
      + '<option value="__new__">➕ Neu …</option>';
    projSel.value = cur;
  }
  document.getElementById('modalOverlay').classList.add('show');
  setTimeout(() => document.getElementById('fTitle')?.focus(), 100);
}

async function createTask() {
  const title = document.getElementById('fTitle').value.trim();
  if (!title) { alert('Titel ist Pflicht!'); return; }
  const projSel = document.getElementById('fProject');
  const projNew = document.getElementById('fProjectNew');
  const project = projNew.style.display !== 'none' && projNew.value.trim()
    ? projNew.value.trim() : projSel.value;
  await api('/api/tasks', {method:'POST', body:JSON.stringify({
    title,
    body: document.getElementById('fBody').value.trim(),
    priority: document.getElementById('fPrio').value,
    project,
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
    <label>📁 Projekt</label>
    <select id="fProject" onchange="if(this.value==='__new__'){this.style.display='none';document.getElementById('fProjectNew').style.display='block';document.getElementById('fProjectNew').focus()}">
      <option value="">— Keins —</option>
      <option value="__new__">➕ Neu …</option>
    </select>
    <input type="text" id="fProjectNew" style="display:none" placeholder="Neues Projekt …">
    <div class="row2">
      <div><label>Status</label>
        <select id="fStatus" onchange="changeStatus('${t.id}',this.value)">
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
  // Populate project select and set current value
  const projs2 = [...new Set(allTasks.map(x => x.project_id).filter(Boolean))].sort();
  const projSel2 = document.getElementById('fProject');
  if (projSel2) {
    projSel2.innerHTML = '<option value="">— Keins —</option>'
      + projs2.map(p => '<option value="'+escHtml(p)+'">'+escHtml(p)+'</option>').join('')
      + '<option value="__new__">➕ Neu …</option>';
    projSel2.value = t.project_id || '';
  }
  document.getElementById('modalOverlay').classList.add('show');
  setTimeout(() => document.getElementById('fTitle')?.focus(), 100);
}

async function saveTask(id) {
  const projSel = document.getElementById('fProject');
  const projNew = document.getElementById('fProjectNew');
  const project = projNew && projNew.style.display !== 'none' && projNew.value.trim()
    ? projNew.value.trim() : (projSel ? projSel.value : '');
  await api('/api/tasks/'+id, {method:'PUT', body:JSON.stringify({
    title: document.getElementById('fTitle').value.trim(),
    body: document.getElementById('fBody').value.trim(),
    priority: document.getElementById('fPrio').value,
    project,
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
  const now = Date.now() / 1000;
  const day = 86400;
  const filtered = filter ? allTasks.filter(t => (t.project_id||'') === filter) : allTasks;
  const sorted = [...filtered].sort((a,b) => {
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
      <td><strong>${escHtml(t.title)}</strong></td>
      <td><span style="color:${PRIO_C[prio]};font-weight:600">${prio}</span></td>
      <td>${COL_NAMES[t.status]||t.status}</td>
      <td style="color:#7C3AED;font-weight:500;font-size:12px">${estStr}</td>
      <td style="font-size:12px">${escHtml(t.project_id||'—')}</td>
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
      <div class="timeline-label" title="${escHtml(t.title)}">${escHtml(t.title)}</div>
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
}

// ── Brainstorm ──
async function submitBrainstorm() {
  const content = document.getElementById('bsInput').value.trim();
  if (!content) return;
  const project = document.getElementById('bsProject').value.trim();
  await api('/api/brainstorm', {method:'POST', body:JSON.stringify({content, project})});
  document.getElementById('bsInput').value = '';
  document.getElementById('bsProject').value = '';
  await loadTasks();
}

function renderBrainstorm() {
  const list = document.getElementById('bsList');
  if (!allBs.length) {
    list.innerHTML = '<div class="empty-state">Noch keine Brainstorm-Einträge. Schreib oben deine Gedanken rein!</div>';
    return;
  }
  const projects = [...new Set(allBs.map(e => e.project).filter(Boolean))];
  document.getElementById('projectList').innerHTML = projects.map(p => `<option value="${escHtml(p)}">`).join('');
  list.innerHTML = allBs.map(e => {
    const date = e.created_at ? new Date(e.created_at * 1000).toLocaleString('de-DE') : '';
    return `<div class="bstorm-entry">
      <div class="bstorm-content">${escHtml(e.content)}</div>
      <div class="bstorm-meta">
        ${e.project ? `<span class="bstorm-project">${escHtml(e.project)}</span>` : ''}
        <span>${date}</span>
        <span style="color:${e.processed ? '#059669' : '#f59e0b'};font-weight:600">${e.processed ? '✅ verarbeitet' : '🟡 offen'}</span>
      </div>
      <div class="bstorm-actions">
        <button class="btn btn-ghost btn-sm" onclick="deleteBrainstorm(${e.id})">🗑</button>
      </div>
    </div>`;
  }).join('');
}

async function deleteBrainstorm(id) {
  if (!confirm('Eintrag löschen?')) return;
  await api('/api/brainstorm/'+id, {method:'DELETE'});
  await loadTasks();
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
    const p = t.project_id || '(ohne Projekt)';
    if (!byProj[p]) byProj[p] = [];
    byProj[p].push(t);
  });
  const projNames = Object.keys(byProj).sort();
  const colors = {'backlog':'#e5e7eb','ready':'#f59e0b','running':'#059669','completed':'#002D69','blocked':'#DC2626'};
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
    html += '<div style="background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
      + '<h3 style="font-size:15px;color:#002D69;font-weight:600">📁 '+escHtml(p)+'</h3>'
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
  const totalDone = allTasks.filter(t => t.status==='completed').length;
  html += '<div style="margin-top:16px;background:#fff;border-radius:10px;padding:14px 16px;display:flex;gap:24px;flex-wrap:wrap;font-size:13px">'
    + '<span>📊 <strong>'+totalTasks+'</strong> Aufgaben</span>'
    + '<span>⏱ <strong>'+_fmtM(totalEst)+'</strong> geschätzt <span style="color:#888">(+ '+_fmtM(totalBuf)+' Puffer)</span></span>'
    + '<span>✅ <strong>'+totalDone+'</strong> erledigt <span style="color:#888">('+(totalTasks?Math.round(totalDone/totalTasks*100):0)+'%)</span></span>'
    + '</div>';
  wrap.innerHTML = html;
}
function _fmtM(m) { if (!m||m<=0)return'0min'; if(m>=60)return Math.floor(m/60)+'h '+(m%60?m%60+'min':''); return m+'min'; }

// ── Calendar View ──
let calWeekOffset = 0;
function getWeekStart(offset) {
  const now = new Date();
  const day = now.getDay();
  const diff = now.getDate() - day + (day === 0 ? -6 : 1) + offset * 7;
  const m = new Date(now.setDate(diff));
  m.setHours(0,0,0,0);
  return m;
}
function renderCalendar() {
  const wrap = document.getElementById('calendarWrap');
  const start = getWeekStart(calWeekOffset);
  const days = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    days.push(d);
  }
  const fmt = d => d.toLocaleDateString('de-DE', {weekday:'short', day:'2-digit', month:'2-digit'});
  const isToday = d => {
    const t = new Date();
    return d.getFullYear()===t.getFullYear() && d.getMonth()===t.getMonth() && d.getDate()===t.getDate();
  };
  document.getElementById('calWeekLabel').textContent =
    fmt(days[0]) + ' – ' + fmt(days[6]);

  // Group tasks by day
  const dayTs = days.map(d => Math.floor(d.getTime()/1000));
  const nextDayTs = days.map(d => Math.floor(new Date(d.getFullYear(),d.getMonth(),d.getDate()+1).getTime()/1000));
  const byDay = days.map(() => []);

  allTasks.forEach(t => {
    const startTs = t.started_at || t.created_at;
    const endTs = t.completed_at || startTs + 86400*14;
    if (!startTs) return;
    for (let i = 0; i < days.length; i++) {
      if (startTs < nextDayTs[i] && endTs >= dayTs[i]) {
        byDay[i].push(t);
      }
    }
  });

  const colors = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};
  let html = '<table style="width:100%;border-collapse:collapse;table-layout:fixed;min-width:700px">'
    + '<thead><tr>';
  days.forEach((d,i) => {
    const today = isToday(d) ? ' style="background:#002D69;color:#fff;border-radius:6px 6px 0 0"' : '';
    html += '<th'+today+' style="padding:8px 6px;font-size:12px;text-align:center;font-weight:600">'+fmt(d)+'</th>';
  });
  html += '</tr></thead><tbody><tr>';
  days.forEach((d,i) => {
    const today = isToday(d) ? ' style="background:#f0f4ff"' : '';
    html += '<td'+today+' style="vertical-align:top;padding:4px;border:1px solid #e5e7eb;height:120px;width:14.28%">';
    if (byDay[i].length) {
      byDay[i].forEach(t => {
        const prio = PRIO_LABEL(t.priority);
        html += '<div style="background:'+(colors[prio]||'#999')+';color:#fff;border-radius:4px;padding:2px 4px;margin-bottom:2px;font-size:10px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" onclick="editTask(\''+t.id+'\')">'+escHtml(t.title)+'</div>';
      });
    }
    html += '</td>';
  });
  html += '</tr></tbody></table>';
  html += '<div style="margin-top:8px;font-size:11px;color:#888;display:flex;gap:12px;flex-wrap:wrap">';
  Object.entries(colors).forEach(([p,c]) => {
    const cnt = allTasks.filter(t => PRIO_LABEL(t.priority)===p).length;
    html += '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:'+c+';margin-right:4px;vertical-align:middle"></span>'+p+' ('+cnt+')</span>';
  });
  html += '</div>';
  wrap.innerHTML = html;
}
function prevWeek() { calWeekOffset--; renderCalendar(); }
function nextWeek() { calWeekOffset++; renderCalendar(); }
function todayWeek() { calWeekOffset=0; renderCalendar(); }

// ── Tabs ──
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const tab = document.querySelector(`.tab[onclick*="'${name}'"]`);
  if (tab) tab.classList.add('active');
  const panel = document.getElementById('panel-' + name);
  if (panel) panel.classList.add('active');
  if (name === 'gantt') renderGantt();
  if (name === 'brainstorm') renderBrainstorm();
  if (name === 'overview') renderOverview();
  if (name === 'calendar') renderCalendar();
}

// Restore title when timer done
setInterval(() => {
  if (pomoState === 'idle' || pomoState === 'paused') {
    document.title = 'Projekte · Christian Radden';
  }
}, 5000);

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