#!/usr/bin/env python3
"""Interaktives Kanban + Gantt — API-Server + Frontend.

Start:  python3 server.py
Dann:   http://localhost:8089
"""

import json
import sqlite3
import uuid
import time
import os
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

HOME = Path.home()
KANBAN_DB = HOME / ".hermes" / "kanban" / "boards" / "christian" / "kanban.db"
PORT = 8089
HOST = "127.0.0.1"

PRIO_LABELS = {1: "hoch", 2: "mittel", 3: "niedrig"}
PRIO_VALUES = {"hoch": 1, "mittel": 2, "niedrig": 3}

STATUS_ORDER = ["backlog", "ready", "running", "completed", "blocked"]
STATUS_NAMES = {
    "backlog": "📋 Backlog",
    "ready": "🟡 Bereit",
    "running": "🟢 In Arbeit",
    "completed": "✅ Erledigt",
    "blocked": "🔴 Blockiert",
}


# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(str(KANBAN_DB))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def dict_row(row):
    return dict(row) if row else None


def fetch_tasks():
    con = get_db()
    rows = con.execute(
        "SELECT id, title, body, status, priority, created_at, started_at, "
        "completed_at, assignee FROM tasks ORDER BY priority, created_at"
    ).fetchall()
    con.close()
    return [dict_row(r) for r in rows]


def fetch_task(task_id):
    con = get_db()
    row = con.execute(
        "SELECT id, title, body, status, priority, created_at, started_at, "
        "completed_at, assignee FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    con.close()
    return dict_row(row)


# ─── API Handler ──────────────────────────────────────────────────────────────

class KanbanHandler(SimpleHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            self.send_html(INDEX_HTML)
        elif self.path == "/api/tasks":
            self.send_json({"tasks": fetch_tasks()})
        elif self.path.startswith("/api/tasks/"):
            task_id = self.path.split("/api/tasks/")[1].split("/")[0]
            if task_id:
                t = fetch_task(task_id)
                self.send_json(t if t else {"error": "not found"}, 200 if t else 404)
            else:
                self.send_json({"error": "missing id"}, 400)
        else:
            self.send_html(INDEX_HTML)  # SPA fallback

    def do_POST(self):
        body = self._read_body()
        path = self.path.rstrip("/")

        if path == "/api/tasks":
            title = body.get("title", "Neue Aufgabe").strip()
            if not title:
                title = "Neue Aufgabe"
            prio = PRIO_VALUES.get(body.get("priority", "mittel"), 2)
            desc = body.get("body", "").strip()
            status = body.get("status", "ready")
            if status not in STATUS_ORDER:
                status = "ready"

            task_id = f"t_{uuid.uuid4().hex[:8]}"
            now = int(time.time())
            con = get_db()
            con.execute(
                "INSERT INTO tasks (id, title, body, status, priority, created_at, assignee) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, desc, status, prio, now, "user"),
            )
            con.commit()
            con.close()
            self.send_json({"ok": True, "id": task_id})

        elif path.startswith("/api/tasks/") and path.endswith("/status"):
            task_id = path.split("/api/tasks/")[1].split("/status")[0]
            new_status = body.get("status", "")
            if new_status not in STATUS_ORDER:
                self.send_json({"error": f"invalid status: {new_status}"}, 400)
                return

            now = int(time.time())
            con = get_db()
            if new_status == "running":
                con.execute("UPDATE tasks SET status=?, started_at=? WHERE id=?", (new_status, now, task_id))
            elif new_status == "completed":
                con.execute("UPDATE tasks SET status=?, completed_at=? WHERE id=?", (new_status, now, task_id))
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

            title = body.get("title", "").strip()
            desc = body.get("body", "").strip()
            prio_str = body.get("priority", "")
            prio = PRIO_VALUES.get(prio_str) if prio_str else None

            updates = []
            params = []
            if title:
                updates.append("title=?")
                params.append(title)
            if desc:
                updates.append("body=?")
                params.append(desc)
            if prio:
                updates.append("priority=?")
                params.append(prio)

            if not updates:
                self.send_json({"ok": True})
                return

            params.append(task_id)
            con = get_db()
            con.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)
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

    # ── helpers ──

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
        pass  # quiet


# ─── Frontend (embedded) ──────────────────────────────────────────────────────

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

/* Tabs */
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:2px solid #e5e7eb}
.tab{padding:10px 20px;cursor:pointer;font-size:14px;font-weight:600;background:transparent;color:#888;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;transition:.2s}
.tab:hover{color:#002D69}
.tab.active{color:#002D69;border-bottom-color:#002D69}
.panel{display:none}
.panel.active{display:block}

/* Toolbar */
.toolbar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.btn{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:.2s;display:inline-flex;align-items:center;gap:4px}
.btn-primary{background:#002D69;color:#fff}
.btn-primary:hover{background:#003d8f}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-danger{background:#DC2626;color:#fff}
.btn-danger:hover{background:#B91C1C}
.btn-ghost{background:transparent;color:#666}
.btn-ghost:hover{background:#e5e7eb;color:#333}

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
.card-actions{position:absolute;top:6px;right:6px;display:flex;gap:2px}
.card-actions button{background:none;border:none;cursor:pointer;font-size:14px;color:#999;padding:2px 4px;border-radius:4px;transition:.2s}
.card-actions button:hover{background:#f0f0f0;color:#333}
.status-select{font-size:10px;padding:2px 4px;border:1px solid #ddd;border-radius:4px;background:#fff;cursor:pointer;color:#555}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:1000;justify-content:center;align-items:center;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:#fff;border-radius:12px;padding:24px;max-width:520px;width:100%;max-height:85vh;overflow-y:auto;box-shadow:0 4px 24px rgba(0,0,0,.2);animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.modal-close{float:right;font-size:22px;cursor:pointer;color:#999;line-height:1}
.modal-close:hover{color:#333}
.modal h2{color:#002D69;font-size:18px;margin-bottom:16px;padding-right:30px}
.modal label{display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px;margin-top:12px}
.modal input[type=text],.modal textarea,.modal select{width:100%;padding:8px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;font-family:inherit}
.modal textarea{min-height:80px;resize:vertical}
.modal .btn-row{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}

/* Gantt */
.gantt{overflow-x:auto;background:#fff;border-radius:10px;padding:16px}
.gantt-table{width:100%;border-collapse:collapse;font-size:13px;min-width:700px}
.gantt-table th{background:#002D69;color:#fff;padding:8px 12px;text-align:left;font-size:12px;position:sticky;top:0}
.gantt-table td{padding:8px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
.gantt-bar-wrap{display:flex;align-items:center;gap:6px}
.gantt-bar{display:block;height:22px;border-radius:4px;color:#fff;font-size:10px;font-weight:600;line-height:22px;padding:0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:12px;transition:width .3s}
.gantt-date{font-size:10px;color:#999;white-space:nowrap}
.gantt-timeline{position:relative;margin-top:16px;padding-top:12px;border-top:1px solid #e5e7eb}
.gantt-timeline .tl-label{font-size:11px;font-weight:600;color:#666;margin-bottom:6px}
.timeline-row{display:flex;align-items:center;margin-bottom:4px;position:relative}
.timeline-label{width:140px;font-size:12px;font-weight:500;color:#333;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:8px;flex-shrink:0}
.timeline-track{flex:1;height:24px;background:#f0f0f0;border-radius:4px;position:relative;overflow:hidden}
.timeline-fill{position:absolute;top:0;left:0;height:100%;border-radius:4px;min-width:4px;transition:width .5s}
.timeline-text{position:absolute;top:0;left:4px;height:100%;display:flex;align-items:center;font-size:10px;color:#fff;font-weight:600;white-space:nowrap}

/* Empty state */
.empty-state{padding:40px 20px;text-align:center;color:#aaa;font-size:14px}

/* Loading */
.loading{text-align:center;padding:40px;color:#888;font-size:14px}
.spinner{display:inline-block;width:20px;height:20px;border:3px solid #e5e7eb;border-top-color:#002D69;border-radius:50%;animation:spin .6s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:1000px){.kanban{grid-template-columns:repeat(3,1fr)}}
@media(max-width:700px){.kanban{grid-template-columns:repeat(2,1fr)}
.timeline-label{width:100px;font-size:11px}}
@media(max-width:500px){.kanban{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>Projekte <span>· Christian Radden</span></h1>
<div class="sub" id="statusLine">Lade Daten …</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('kanban')">📋 Kanban</button>
  <button class="tab" onclick="switchTab('gantt')">📊 Gantt</button>
</div>

<div id="panel-kanban" class="panel active">
  <div class="toolbar">
    <button class="btn btn-primary" onclick="openCreateModal()">+ Neue Karte</button>
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="kanban" class="kanban"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<div id="panel-gantt" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="loadTasks()">🔄 Aktualisieren</button>
  </div>
  <div id="ganttWrap" class="gantt"><div class="loading"><span class="spinner"></span>Lade …</div></div>
</div>

<!-- Modal -->
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modalContent">
    <span class="modal-close" onclick="closeModal()">&times;</span>
    <h2 id="modalTitle">Aufgabe</h2>
    <div id="modalBody"></div>
  </div>
</div>

<script>
// ── State ──
let allTasks = [];

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
    const data = await api('/api/tasks');
    allTasks = data.tasks || [];
    document.getElementById('statusLine').textContent =
      allTasks.length + ' Aufgaben · ' + new Date().toLocaleTimeString('de-DE');
    renderKanban();
    renderGantt();
  } catch(e) {
    document.getElementById('statusLine').textContent = '⚠️ Fehler beim Laden';
    console.error(e);
  }
}

// ── Kanban ──
const COLS = ['backlog','ready','running','completed','blocked'];
const COL_NAMES = {'backlog':'📋 Backlog','ready':'🟡 Bereit','running':'🟢 In Arbeit','completed':'✅ Erledigt','blocked':'🔴 Blockiert'};
const PRIO_C = {'hoch':'#DD3221','mittel':'#f59e0b','niedrig':'#6b7280'};

function renderKanban() {
  const kanban = document.getElementById('kanban');
  kanban.innerHTML = COLS.map(col => {
    const items = allTasks.filter(t => t.status === col);
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
  return `<div class="card prio-${prio}" onclick="editTask('${t.id}')">
    <div class="card-actions" onclick="event.stopPropagation()">
      <button onclick="deleteTask('${t.id}')" title="Löschen">🗑</button>
    </div>
    <div class="card-title">${escHtml(t.title)}</div>
    ${bodyShort ? `<div class="card-body">${escHtml(bodyShort)}</div>` : ''}
    <div class="card-meta">
      <span style="color:${PRIO_C[prio]||'#999'};font-weight:600">${prio}</span>
      <select class="status-select" onchange="changeStatus('${t.id}',this.value)" onclick="event.stopPropagation()">
        ${COLS.map(s => `<option value="${s}" ${s===t.status?'selected':''}>${s}</option>`).join('')}
      </select>
    </div>
  </div>`;
}

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

// ── Status change ──
async function changeStatus(id, newStatus) {
  await api(`/api/tasks/${id}/status`, {
    method: 'POST',
    body: JSON.stringify({status: newStatus})
  });
  await loadTasks();
}

// ── Delete ──
async function deleteTask(id) {
  if (!confirm('Aufgabe löschen?')) return;
  await api(`/api/tasks/${id}`, {method: 'DELETE'});
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
    <label>Priorität</label>
    <select id="fPrio">
      <option value="hoch">🔴 Hoch</option>
      <option value="mittel" selected>🟡 Mittel</option>
      <option value="niedrig">🟢 Niedrig</option>
    </select>
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
    </div>
  `;
  document.getElementById('modalOverlay').classList.add('show');
  setTimeout(() => document.getElementById('fTitle')?.focus(), 100);
}

async function createTask() {
  const title = document.getElementById('fTitle').value.trim();
  if (!title) { alert('Titel ist Pflicht!'); return; }
  await api('/api/tasks', {
    method: 'POST',
    body: JSON.stringify({
      title,
      body: document.getElementById('fBody').value.trim(),
      priority: document.getElementById('fPrio').value,
      status: document.getElementById('fStatus').value,
    })
  });
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
    <label>Priorität</label>
    <select id="fPrio">
      <option value="hoch" ${prio==='hoch'?'selected':''}>🔴 Hoch</option>
      <option value="mittel" ${prio==='mittel'?'selected':''}>🟡 Mittel</option>
      <option value="niedrig" ${prio==='niedrig'?'selected':''}>🟢 Niedrig</option>
    </select>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="closeModal()">Abbrechen</button>
      <button class="btn btn-danger" onclick="deleteTask('${t.id}')">🗑 Löschen</button>
      <button class="btn btn-primary" onclick="saveTask('${t.id}')">Speichern</button>
    </div>
  `;
  document.getElementById('modalOverlay').classList.add('show');
  setTimeout(() => document.getElementById('fTitle')?.focus(), 100);
}

async function saveTask(id) {
  const title = document.getElementById('fTitle').value.trim();
  const body = document.getElementById('fBody').value.trim();
  const prio = document.getElementById('fPrio').value;
  await api(`/api/tasks/${id}`, {
    method: 'PUT',
    body: JSON.stringify({title, body, priority: prio})
  });
  closeModal();
  await loadTasks();
}

function closeModal() {
  document.getElementById('modalOverlay').classList.remove('show');
}

// ── Gantt ──
function renderGantt() {
  const wrap = document.getElementById('ganttWrap');
  if (!allTasks.length) {
    wrap.innerHTML = '<div class="empty-state">Keine Aufgaben vorhanden</div>';
    return;
  }

  const now = Date.now() / 1000;
  const day = 86400;

  // Sort by priority then created_at
  const sorted = [...allTasks].sort((a,b) => {
    const pa = typeof a.priority === 'number' ? a.priority : PRIO_VALUES[a.priority] || 2;
    const pb = typeof b.priority === 'number' ? b.priority : PRIO_VALUES[b.priority] || 2;
    if (pa !== pb) return pa - pb;
    return (a.created_at||0) - (b.created_at||0);
  });

  // Calculate start and end timestamps
  const tasks = sorted.map(t => {
    const start = t.started_at || t.created_at || now;
    let end = t.completed_at || now;
    // If not completed, estimate end from now + priority-based duration
    if (!t.completed_at && t.status !== 'completed') {
      end = now + (PRIO_VALUES[PRIO_LABEL(t.priority)] === 1 ? 7*day : PRIO_VALUES[PRIO_LABEL(t.priority)] === 2 ? 14*day : 30*day);
    }
    return {...t, _start: start, _end: end};
  });

  const minT = Math.min(...tasks.map(t => t._start));
  const maxT = Math.max(...tasks.map(t => t._end), now + 7*day);
  const range = maxT - minT || day;

  function pct(val) { return Math.max(2, ((val - minT) / range) * 100); }

  let html = '<table class="gantt-table"><thead><tr><th>Projekt</th><th>Prio</th><th>Status</th><th>Start</th><th>Ende</th></tr></thead><tbody>';
  tasks.forEach(t => {
    const prio = PRIO_LABEL(t.priority);
    const pctDone = t.status === 'completed' ? 100 : t.status === 'running' ? 60 : t.status === 'blocked' ? 5 : 10;
    html += `<tr>
      <td><strong>${escHtml(t.title)}</strong></td>
      <td><span style="color:${PRIO_C[prio]};font-weight:600">${prio}</span></td>
      <td>${COL_NAMES[t.status]||t.status}</td>
      <td class="gantt-date">${t._start ? fmtDate(t._start) : '—'}</td>
      <td class="gantt-date">${t.completed_at ? fmtDate(t.completed_at) : (t.status==='completed'?'—':'offen')}</td>
    </tr>`;
  });
  html += '</tbody></table>';

  // Timeline view
  html += '<div class="gantt-timeline"><div class="tl-label">📈 Zeitverlauf</div>';
  tasks.forEach(t => {
    const prio = PRIO_LABEL(t.priority);
    const left = pct(t._start);
    const width = Math.max(3, pct(t._end) - left);
    html += `<div class="timeline-row">
      <div class="timeline-label" title="${escHtml(t.title)}">${escHtml(t.title)}</div>
      <div class="timeline-track">
        <div class="timeline-fill" style="left:${left}%;width:${width}%;background:${PRIO_C[prio]};opacity:${t.status==='completed'?'0.6':'1'}"></div>
        <div class="timeline-text" style="left:${left}%;width:${width}%">${escHtml(t.title)}</div>
      </div>
    </div>`;
  });

  // Today marker
  const todayPct = pct(now);
  html += `<div style="position:relative;height:0;margin-top:-4px">
    <div style="position:absolute;left:${todayPct}%;top:0;width:2px;height:${tasks.length*28+12}px;background:#DD3221;z-index:2" title="Heute"></div>
    <div style="position:absolute;left:${todayPct}%;top:-2px;font-size:10px;color:#DD3221;font-weight:600;transform:translateX(-50%)">▼ Heute</div>
  </div>`;

  html += '</div>';
  wrap.innerHTML = html;
}

function fmtDate(ts) {
  const d = new Date((typeof ts === 'number' ? ts : parseInt(ts)) * 1000);
  return d.toLocaleDateString('de-DE', {day:'2-digit',month:'2-digit',year:'2-digit'});
}

// ── Tabs ──
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelector(`.tab[onclick*="'${name}'"]`)?.classList.add('active');
  document.getElementById('panel-' + name)?.classList.add('active');
  if (name === 'gantt') renderGantt();
}

// ── Init ──
loadTasks();
</script>
</body>
</html>
"""

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), KanbanHandler)
    print(f"✅ Kanban + Gantt Server: http://{HOST}:{PORT}")
    print(f"   Dashboard:            http://{HOST}:{PORT}/")
    print(f"   API:                  http://{HOST}:{PORT}/api/tasks")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\nServer gestoppt.")