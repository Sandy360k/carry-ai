"""
carry-ai/ui/app.py — Flask Web UI
====================================

Local web interface served at http://localhost:8080. Provides a
browser-based chat experience with real-time streaming responses.

Architecture:
    - Flask backend serves API + inline HTML/CSS/JS (no build step)
    - SSE (Server-Sent Events) for streaming responses
    - Single-page app — all vanilla JS, no frameworks needed
"""

import json
import logging
import sys
import time

try:
    from flask import Flask, Response, request, jsonify
except ImportError:
    Flask = None

log = logging.getLogger("carry-ai.ui")


# ===================================================================
# HTML template (embedded — no external files needed on USB)
# ===================================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>carry-ai</title>
<style>
:root { --bg: #1a1a2e; --bg2: #16213e; --bg3: #12112a; --fg: #e0e0e0; --fg2: #a0a0a0;
  --accent: #0f3460; --accent2: #533483; --green: #4ecca3; --red: #e94560; --yellow: #f0c040;
  --border: #2a2a4a; --msg-user: #1a3a5c; --msg-ai: #1e1e3a;
  --code-bg: #0d1117; --radius: 8px; --font: 'Segoe UI', system-ui, sans-serif;
  --mono: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
  --sidebar-w: 220px; }
[data-theme="light"] { --bg: #f5f5f5; --bg2: #ffffff; --bg3: #eaeaea; --fg: #1a1a1a; --fg2: #666;
  --accent: #e3f2fd; --border: #ddd; --msg-user: #e3f2fd; --msg-ai: #f0f0f0;
  --code-bg: #f6f8fa; --yellow: #b08800; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font); background: var(--bg); color: var(--fg);
  height: 100vh; display: flex; overflow: hidden; }

/* --- Sidebar (inspired by Skales app-shell) --- */
#sidebar { width: var(--sidebar-w); background: var(--bg3); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; flex-shrink: 0; transition: width 0.2s; overflow: hidden; }
#sidebar.collapsed { width: 48px; }
#sidebar .brand { padding: 12px 14px; font-weight: 700; font-size: 15px; display: flex;
  align-items: center; gap: 8px; border-bottom: 1px solid var(--border); cursor: pointer; }
#sidebar .brand .logo { width: 22px; height: 22px; background: var(--green); border-radius: 6px;
  display: flex; align-items: center; justify-content: center; font-size: 12px; color: #111; font-weight: 800; }
#sidebar.collapsed .brand-text { display: none; }
#sidebar nav { flex: 1; padding: 8px 0; overflow-y: auto; }
#sidebar nav a { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
  color: var(--fg2); text-decoration: none; font-size: 13px; border-left: 3px solid transparent;
  transition: all 0.15s; cursor: pointer; }
#sidebar nav a:hover { color: var(--fg); background: rgba(255,255,255,0.04); }
#sidebar nav a.active { color: var(--green); border-left-color: var(--green); background: rgba(78,204,163,0.08); }
#sidebar nav a .icon { width: 18px; text-align: center; font-size: 15px; flex-shrink: 0; }
#sidebar.collapsed nav a span:not(.icon) { display: none; }
#sidebar .sidebar-footer { padding: 8px 14px; border-top: 1px solid var(--border); font-size: 11px; color: var(--fg2); }
#sidebar.collapsed .sidebar-footer span { display: none; }

/* --- Main content area --- */
#main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
header { background: var(--bg2); border-bottom: 1px solid var(--border);
  padding: 8px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
header h1 { font-size: 15px; font-weight: 600; }
header .status { font-size: 12px; color: var(--fg2); margin-left: auto; }
header .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
header .dot.on { background: var(--green); } header .dot.off { background: var(--red); }
.mode-badge { font-size: 11px; padding: 2px 8px; border-radius: 10px;
  background: var(--accent); color: var(--green); font-weight: 600; text-transform: uppercase; }
#theme-toggle, .hdr-btn { background: none; border: 1px solid var(--border); color: var(--fg2);
  padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 12px; }

/* --- Panels (chat, memory, tools, settings, logs) --- */
.panel { flex: 1; overflow-y: auto; display: none; flex-direction: column; }
.panel.active { display: flex; }

/* Chat panel */
#chat-panel { padding: 0; }
#chat { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.msg { max-width: 85%; padding: 10px 14px; border-radius: var(--radius);
  font-size: 14px; line-height: 1.6; word-wrap: break-word; white-space: pre-wrap; }
.msg.user { align-self: flex-end; background: var(--msg-user); border-bottom-right-radius: 2px; }
.msg.ai { align-self: flex-start; background: var(--msg-ai); border-bottom-left-radius: 2px; }
.msg.ai code { background: var(--code-bg); padding: 1px 4px; border-radius: 3px;
  font-family: var(--mono); font-size: 13px; }
.msg.ai pre { background: var(--code-bg); padding: 10px; border-radius: 6px;
  overflow-x: auto; margin: 8px 0; }
.msg.ai pre code { background: none; padding: 0; }
.msg .tool-badge { display: inline-block; font-size: 11px; padding: 1px 6px;
  border-radius: 4px; background: var(--accent2); color: #ccc; margin: 4px 4px 4px 0; cursor: pointer; }
.msg .tool-output { font-size: 12px; color: var(--fg2); border-left: 2px solid var(--border);
  padding-left: 8px; margin: 6px 0; max-height: 200px; overflow-y: auto;
  font-family: var(--mono); white-space: pre-wrap; }
.tool-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px;
  margin: 6px 0; overflow: hidden; }
.tool-card .tool-header { padding: 4px 10px; font-size: 12px; display: flex; align-items: center;
  gap: 6px; cursor: pointer; color: var(--fg2); }
.tool-card .tool-header:hover { color: var(--fg); }
.tool-card .tool-body { display: none; padding: 8px 10px; border-top: 1px solid var(--border);
  font-family: var(--mono); font-size: 12px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
.tool-card.open .tool-body { display: block; }
.typing { color: var(--fg2); font-style: italic; font-size: 13px;
  align-self: flex-start; padding: 4px 14px; }

footer { background: var(--bg2); border-top: 1px solid var(--border);
  padding: 12px 16px; flex-shrink: 0; }
#input-row { display: flex; gap: 8px; max-width: 900px; margin: 0 auto; }
#msg-input { flex: 1; padding: 10px 14px; border: 1px solid var(--border);
  border-radius: var(--radius); background: var(--bg); color: var(--fg);
  font-size: 14px; font-family: var(--font); resize: none; min-height: 42px;
  max-height: 150px; outline: none; }
#msg-input:focus { border-color: var(--green); }
#send-btn { padding: 10px 20px; background: var(--green); color: #111; border: none;
  border-radius: var(--radius); font-weight: 600; cursor: pointer; font-size: 14px; }
#send-btn:hover { opacity: 0.9; } #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* Info panels (memory, tools, settings, logs) */
.info-panel { padding: 20px; }
.info-panel h2 { font-size: 16px; margin-bottom: 12px; color: var(--green); }
.info-panel .card { background: var(--bg2); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px; margin-bottom: 10px; }
.info-panel .card h3 { font-size: 13px; color: var(--fg2); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
.info-panel table { width: 100%; border-collapse: collapse; font-size: 13px; }
.info-panel table th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); color: var(--fg2); font-weight: 500; }
.info-panel table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.info-panel input, .info-panel select { background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  padding: 6px 10px; border-radius: 4px; font-size: 13px; width: 100%; }
.info-panel .btn { padding: 6px 14px; background: var(--accent); color: var(--green); border: 1px solid var(--border);
  border-radius: 4px; cursor: pointer; font-size: 12px; }
.info-panel .btn:hover { opacity: 0.8; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px;
  background: var(--accent); color: var(--green); margin: 1px; }

@media (max-width: 768px) {
  #sidebar { width: 48px; }
  #sidebar nav a span:not(.icon) { display: none; }
  #sidebar .brand-text, #sidebar .sidebar-footer span { display: none; }
  .msg { max-width: 95%; }
}
</style>
</head>
<body>

<!-- Sidebar (inspired by Skales app-shell + sidebar pattern) -->
<div id="sidebar">
  <div class="brand" onclick="toggleSidebar()">
    <div class="logo">AI</div>
    <span class="brand-text">carry-ai</span>
  </div>
  <nav>
    <a class="active" onclick="switchPanel('chat-panel')" data-panel="chat-panel">
      <span class="icon">&#128172;</span><span>Chat</span></a>
    <a onclick="switchPanel('memory-panel')" data-panel="memory-panel">
      <span class="icon">&#129504;</span><span>Memory</span></a>
    <a onclick="switchPanel('tools-panel')" data-panel="tools-panel">
      <span class="icon">&#128295;</span><span>Tools</span></a>
    <a onclick="switchPanel('settings-panel')" data-panel="settings-panel">
      <span class="icon">&#9881;</span><span>Settings</span></a>
    <a onclick="switchPanel('logs-panel')" data-panel="logs-panel">
      <span class="icon">&#128196;</span><span>Logs</span></a>
  </nav>
  <div class="sidebar-footer">
    <span id="sidebar-stats">--</span>
  </div>
</div>

<!-- Main content -->
<div id="main">
<header>
  <h1 id="panel-title">Chat</h1>
  <span class="mode-badge" id="mode-badge">--</span>
  <span class="status">
    <span class="dot" id="status-dot"></span>
    <span id="status-text">connecting...</span>
  </span>
  <button id="theme-toggle" onclick="toggleTheme()">theme</button>
</header>

<!-- Chat Panel -->
<div class="panel active" id="chat-panel">
  <div id="chat"></div>
</div>

<!-- Memory Panel -->
<div class="panel info-panel" id="memory-panel">
  <h2>Memory</h2>
  <div style="display:flex;gap:8px;margin-bottom:12px;">
    <input type="text" id="mem-search" placeholder="Search memories..." style="flex:1">
    <button class="btn" onclick="searchMemory()">Search</button>
  </div>
  <div id="mem-stats" class="card"><h3>Stats</h3><div id="mem-stats-body">Loading...</div></div>
  <div id="mem-entries"></div>
</div>

<!-- Tools Panel -->
<div class="panel info-panel" id="tools-panel">
  <h2>Registered Tools</h2>
  <div id="tools-list">Loading...</div>
</div>

<!-- Settings Panel -->
<div class="panel info-panel" id="settings-panel">
  <h2>Settings</h2>
  <div class="card">
    <h3>Mode</h3>
    <div style="display:flex;gap:8px;margin-top:6px;">
      <button class="btn" onclick="switchMode('local')">Local</button>
      <button class="btn" onclick="switchMode('api')">API</button>
      <button class="btn" onclick="switchMode('hybrid')">Hybrid</button>
    </div>
  </div>
  <div class="card">
    <h3>Providers</h3>
    <div id="providers-list">Loading...</div>
  </div>
  <div class="card">
    <h3>Models</h3>
    <div id="models-list">Loading...</div>
  </div>
  <div class="card">
    <h3>Dependencies</h3>
    <div id="deps-list">Loading...</div>
    <div style="margin-top:8px;display:flex;gap:8px;">
      <button class="btn" onclick="updateAllDeps()" id="btn-update-deps">Update All</button>
    </div>
    <div id="deps-status" style="margin-top:6px;font-size:12px;"></div>
  </div>
</div>

<!-- Logs Panel -->
<div class="panel info-panel" id="logs-panel">
  <h2>Activity Log</h2>
  <div id="log-entries" style="font-family:var(--mono);font-size:12px;white-space:pre-wrap;max-height:80vh;overflow-y:auto;"></div>
</div>

<footer>
  <div id="input-row">
    <textarea id="msg-input" rows="1" placeholder="Type a message..." autofocus></textarea>
    <button id="send-btn" onclick="sendMessage()">Send</button>
  </div>
</footer>
</div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('msg-input');
const sendBtn = document.getElementById('send-btn');
let sending = false;
const activityLog = [];

// --- Sidebar & Panel switching (Skales-inspired) ---
function switchPanel(panelId) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#sidebar nav a').forEach(a => a.classList.remove('active'));
  const panel = document.getElementById(panelId);
  if (panel) panel.classList.add('active');
  const link = document.querySelector(`[data-panel="${panelId}"]`);
  if (link) link.classList.add('active');
  const titles = {'chat-panel':'Chat','memory-panel':'Memory','tools-panel':'Tools','settings-panel':'Settings','logs-panel':'Logs'};
  document.getElementById('panel-title').textContent = titles[panelId] || 'carry-ai';
  // Load panel data
  if (panelId === 'memory-panel') loadMemory();
  if (panelId === 'tools-panel') loadTools();
  if (panelId === 'settings-panel') { loadSettings(); loadDeps(); }
  if (panelId === 'logs-panel') renderLogs();
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
}

// --- Activity logging ---
function logActivity(msg) {
  const ts = new Date().toLocaleTimeString();
  activityLog.push(`[${ts}] ${msg}`);
  if (activityLog.length > 200) activityLog.shift();
}
function renderLogs() {
  document.getElementById('log-entries').textContent = activityLog.join('\n') || '(no activity yet)';
}

// --- Chat ---
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 150) + 'px';
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

function addMsg(role, html) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = html;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}
function escHtml(s) {
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}
function formatContent(text) {
  text = escHtml(text);
  // Code blocks with language label
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => {
    const label = lang ? `<div style="font-size:11px;color:var(--fg2);padding:2px 10px;border-bottom:1px solid var(--border)">${lang}</div>` : '';
    return `<pre>${label}<code>${code}</code></pre>`;
  });
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Headings
  text = text.replace(/^### (.+)$/gm, '<strong style="font-size:14px">$1</strong>');
  text = text.replace(/^## (.+)$/gm, '<strong style="font-size:15px;color:var(--green)">$1</strong>');
  // Bold, italic
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Lists
  text = text.replace(/^- (.+)$/gm, '&bull; $1');
  // Links
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" style="color:var(--green)">$1</a>');
  return text;
}

async function sendMessage() {
  const text = input.value.trim();
  if (!text || sending) return;
  sending = true; sendBtn.disabled = true;
  input.value = ''; input.style.height = 'auto';

  addMsg('user', escHtml(text));
  logActivity('User: ' + text.slice(0, 80));
  const aiDiv = addMsg('ai', '');
  const typingEl = document.createElement('div');
  typingEl.className = 'typing'; typingEl.textContent = 'thinking...';
  chat.appendChild(typingEl);
  chat.scrollTop = chat.scrollHeight;

  let fullText = '';
  let toolCards = '';
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, {stream: true});
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const dataStr = line.slice(6);
        if (dataStr === '[DONE]') continue;
        let evt;
        try { evt = JSON.parse(dataStr); } catch { continue; }
        if (evt.type === 'text') {
          fullText += evt.data;
          aiDiv.innerHTML = formatContent(fullText) + toolCards;
          chat.scrollTop = chat.scrollHeight;
        } else if (evt.type === 'tool_start') {
          const name = escHtml(evt.data.name);
          toolCards += `<div class="tool-card" onclick="this.classList.toggle('open')">` +
            `<div class="tool-header"><span class="tool-badge">${name}</span> running...</div>` +
            `<div class="tool-body">...</div></div>`;
          aiDiv.innerHTML = formatContent(fullText) + toolCards;
          logActivity('Tool: ' + name);
        } else if (evt.type === 'tool_result') {
          const result = escHtml((evt.data.result||'').slice(0,1000));
          // Update the last tool card
          const cards = aiDiv.querySelectorAll('.tool-card');
          if (cards.length) {
            const last = cards[cards.length-1];
            last.querySelector('.tool-header').innerHTML = last.querySelector('.tool-header').innerHTML.replace('running...','done');
            last.querySelector('.tool-body').textContent = evt.data.result||'';
          }
          toolCards = aiDiv.innerHTML.replace(formatContent(fullText), '');
        }
      }
    }
    if (!fullText && !toolCards) aiDiv.innerHTML = '<span style="color:var(--fg2)">(no response)</span>';
    logActivity('AI: ' + (fullText||'').slice(0, 80));
  } catch (err) {
    aiDiv.innerHTML = '<span style="color:var(--red)">Error: ' + escHtml(err.message) + '</span>';
    logActivity('Error: ' + err.message);
  }
  typingEl.remove();
  sending = false; sendBtn.disabled = false;
  input.focus();
}

// --- Memory panel ---
async function loadMemory() {
  try {
    const resp = await fetch('/api/memory');
    const data = await resp.json();
    const stats = data.stats || {};
    document.getElementById('mem-stats-body').innerHTML =
      `Observations: <strong>${stats.total_observations||0}</strong> | ` +
      `Sessions: <strong>${stats.total_sessions||0}</strong> | ` +
      `Projects: <strong>${stats.total_projects||0}</strong>`;
    const entries = data.entries || [];
    const html = entries.map(e => `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong>${escHtml(e.title||'')}</strong>
        <span class="tag">${escHtml(e.obs_type||'note')}</span>
      </div>
      <div style="font-size:12px;color:var(--fg2);margin-top:4px">${escHtml((e.content||'').slice(0,200))}</div>
      ${(e.concepts||[]).map(c=>`<span class="tag">${escHtml(c)}</span>`).join(' ')}
    </div>`).join('');
    document.getElementById('mem-entries').innerHTML = html || '<div style="color:var(--fg2)">No memories yet</div>';
  } catch(e) { document.getElementById('mem-entries').innerHTML = 'Error loading memory'; }
}
async function searchMemory() {
  const q = document.getElementById('mem-search').value.trim();
  if (!q) return loadMemory();
  try {
    const resp = await fetch('/api/memory/search?q=' + encodeURIComponent(q));
    const data = await resp.json();
    const results = data.results || [];
    document.getElementById('mem-entries').innerHTML = results.map(e => `<div class="card">
      <strong>${escHtml(e.title||'')}</strong> <span class="tag">${escHtml(e.obs_type||'')}</span>
      <div style="font-size:12px;color:var(--fg2);margin-top:4px">${escHtml((e.content||'').slice(0,200))}</div>
    </div>`).join('') || '<div style="color:var(--fg2)">No results</div>';
  } catch(e) { document.getElementById('mem-entries').innerHTML = 'Search error'; }
}

// --- Tools panel ---
async function loadTools() {
  try {
    const resp = await fetch('/api/tools');
    const tools = await resp.json();
    const categories = {};
    tools.forEach(t => {
      const cat = t.name.includes('_') ? t.name.split('_')[0] : 'general';
      if (!categories[cat]) categories[cat] = [];
      categories[cat].push(t);
    });
    let html = '';
    for (const [cat, catTools] of Object.entries(categories)) {
      html += `<div class="card"><h3>${escHtml(cat)} (${catTools.length})</h3><table>
        <tr><th>Tool</th><th>Description</th><th>Params</th></tr>`;
      catTools.forEach(t => {
        html += `<tr><td><code>${escHtml(t.name)}</code></td>
          <td style="font-size:12px">${escHtml((t.description||'').slice(0,80))}</td>
          <td style="font-size:11px;color:var(--fg2)">${(t.parameters||[]).join(', ')}</td></tr>`;
      });
      html += '</table></div>';
    }
    document.getElementById('tools-list').innerHTML = html || 'No tools';
  } catch(e) { document.getElementById('tools-list').innerHTML = 'Error loading tools'; }
}

// --- Settings panel ---
async function loadSettings() {
  try {
    const [provResp, modResp] = await Promise.all([fetch('/api/providers'), fetch('/api/models')]);
    const prov = await provResp.json();
    const models = await modResp.json();
    document.getElementById('providers-list').innerHTML = (prov.configured||[]).length
      ? (prov.configured||[]).map(p => `<span class="tag">${escHtml(p)}</span>`).join(' ')
      : '<span style="color:var(--fg2)">None configured</span>';
    const avail = models.available || [];
    document.getElementById('models-list').innerHTML = avail.length
      ? `<table><tr><th>Model</th><th>Size</th></tr>` +
        avail.map(m => `<tr><td>${escHtml(m.name)}</td><td>${m.size_gb} GB</td></tr>`).join('') +
        '</table>'
      : '<span style="color:var(--fg2)">No local models</span>';
  } catch(e) { document.getElementById('providers-list').innerHTML = 'Error'; }
}
async function loadDeps() {
  try {
    const resp = await fetch('/api/dependencies');
    const data = await resp.json();
    const pkgs = data.packages || [];
    if (!pkgs.length) { document.getElementById('deps-list').innerHTML = 'No data'; return; }
    let html = '<table><tr><th>Package</th><th>Type</th><th>Status</th><th>Version</th></tr>';
    pkgs.forEach(p => {
      const st = p.installed
        ? '<span style="color:var(--green)">installed</span>'
        : (p.required ? '<span style="color:#e74c3c">missing</span>' : '<span style="color:var(--fg2)">not installed</span>');
      html += `<tr><td><code>${escHtml(p.package)}</code></td>
        <td style="font-size:11px">${p.required?'required':'optional'}</td>
        <td>${st}</td>
        <td style="font-size:11px;color:var(--fg2)">${p.version||'-'}</td></tr>`;
    });
    html += '</table>';
    document.getElementById('deps-list').innerHTML = html;
  } catch(e) { document.getElementById('deps-list').innerHTML = 'Error loading'; }
}
async function updateAllDeps() {
  const btn = document.getElementById('btn-update-deps');
  const status = document.getElementById('deps-status');
  btn.disabled = true; btn.textContent = 'Updating...';
  status.innerHTML = '<span style="color:var(--fg2)">Updating packages, this may take a minute...</span>';
  logActivity('Updating all dependencies...');
  try {
    const resp = await fetch('/api/dependencies/update', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const data = await resp.json();
    const results = data.results || [];
    const ok = results.filter(r => r.success).length;
    const fail = results.filter(r => !r.success).length;
    status.innerHTML = `<span style="color:var(--green)">${ok} updated</span>` +
      (fail ? `, <span style="color:#e74c3c">${fail} failed</span>` : '');
    logActivity(`Dependencies updated: ${ok} ok, ${fail} failed`);
    loadDeps();
  } catch(e) {
    status.innerHTML = '<span style="color:#e74c3c">Update failed: ' + escHtml(e.message) + '</span>';
    logActivity('Dependency update failed: ' + e.message);
  }
  btn.disabled = false; btn.textContent = 'Update All';
}
async function switchMode(mode) {
  try {
    await fetch('/api/mode', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    logActivity('Mode switched to: ' + mode);
    pollStatus();
  } catch(e) { logActivity('Mode switch failed: ' + e.message); }
}

// --- Status polling ---
async function pollStatus() {
  try {
    const resp = await fetch('/api/status');
    const s = await resp.json();
    document.getElementById('mode-badge').textContent = s.mode || '--';
    const dot = document.getElementById('status-dot');
    const stxt = document.getElementById('status-text');
    dot.className = 'dot on'; stxt.textContent = s.model || 'ready';
    document.getElementById('sidebar-stats').textContent =
      `${s.mode||'--'} | ${Math.floor((s.uptime_s||0)/60)}m`;
  } catch {
    document.getElementById('status-dot').className = 'dot off';
    document.getElementById('status-text').textContent = 'offline';
  }
}
pollStatus(); setInterval(pollStatus, 10000);

// Theme toggle
function toggleTheme() {
  const html = document.documentElement;
  html.setAttribute('data-theme', html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
}

// Keyboard shortcut: Ctrl+K to focus search
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); input.focus(); }
});
</script>
</body>
</html>"""


# ===================================================================
# Flask app factory
# ===================================================================

def create_app(agent=None, config=None):
    """Create and configure the Flask application.

    Args:
        agent: Agent instance (or will be created from config).
        config: Boot context dict from launcher.py.

    Returns:
        Flask app instance.
    """
    if Flask is None:
        raise ImportError("Flask not installed. pip install flask")

    config = config or {}
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # Lazy agent initialization
    _agent = {"instance": agent}
    _boot_time = time.time()

    def _get_agent():
        if _agent["instance"] is None:
            from agent.agent import Agent
            _agent["instance"] = Agent(boot_context=config)
        return _agent["instance"]

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        return Response(INDEX_HTML, content_type="text/html")

    # ------------------------------------------------------------------
    # Chat API (SSE streaming)
    # ------------------------------------------------------------------

    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        data = request.get_json(silent=True) or {}
        message = data.get("message", "").strip()

        if not message:
            return jsonify({"error": "Empty message"}), 400

        agent = _get_agent()

        def generate():
            try:
                for chunk in agent.stream_turn(message):
                    payload = json.dumps(chunk)
                    yield f"data: {payload}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                log.error("Stream error: %s", e, exc_info=True)
                err = json.dumps({"type": "text", "data": f"Error: {e}"})
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"

        return Response(
            generate(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------
    # Status API
    # ------------------------------------------------------------------

    @app.route("/api/status")
    def api_status():
        try:
            agent = _get_agent()
            agent_status = agent.get_status()
        except Exception:
            agent_status = {}

        return jsonify({
            "mode": config.get("mode", "unknown"),
            "model": config.get("model_tier", {}).get("name") if isinstance(config.get("model_tier"), dict)
                     else str(config.get("model_path", "API")),
            "uptime_s": int(time.time() - _boot_time),
            "ram_gb": config.get("ram_gb"),
            "dry_run": config.get("dry_run", False),
            "port": config.get("port", 8080),
            "agent": agent_status,
        })

    # ------------------------------------------------------------------
    # Tools API
    # ------------------------------------------------------------------

    @app.route("/api/tools")
    def api_tools():
        from agent.tools import TOOL_REGISTRY
        tools = []
        for name, tool in TOOL_REGISTRY.items():
            tools.append({
                "name": name,
                "description": tool.description,
                "parameters": list(tool.parameters.get("properties", {}).keys()),
            })
        return jsonify(tools)

    # ------------------------------------------------------------------
    # Models API
    # ------------------------------------------------------------------

    @app.route("/api/models")
    def api_models():
        from modes.local_mode import scan_available_models, MODEL_TIERS
        from pathlib import Path

        models_dir = Path(config.get("models_dir", "models"))
        available = scan_available_models(models_dir)
        return jsonify({
            "available": [{"name": m.name, "size_gb": round(m.stat().st_size / (1024**3), 2)}
                          for m in available],
            "tiers": [{"name": t.name, "min_ram_gb": t.min_ram_gb,
                        "context_size": t.context_size}
                       for t in MODEL_TIERS],
        })

    # ------------------------------------------------------------------
    # Providers API
    # ------------------------------------------------------------------

    @app.route("/api/providers")
    def api_providers():
        providers = config.get("api_keys", {})
        return jsonify({
            "configured": [k for k in providers.keys() if not k.startswith("_")]
                          if providers else [],
        })

    # ------------------------------------------------------------------
    # Mode switch
    # ------------------------------------------------------------------

    @app.route("/api/mode", methods=["POST"])
    def api_mode_switch():
        data = request.get_json(silent=True) or {}
        new_mode = data.get("mode", "")
        if new_mode not in ("local", "api", "hybrid"):
            return jsonify({"error": "Invalid mode. Choose: local, api, hybrid"}), 400
        config["mode"] = new_mode
        return jsonify({"mode": new_mode, "status": "switched"})

    # ------------------------------------------------------------------
    # History API
    # ------------------------------------------------------------------

    @app.route("/api/history")
    def api_history():
        try:
            agent = _get_agent()
            return jsonify(agent.get_history())
        except Exception:
            return jsonify([])

    @app.route("/api/history", methods=["DELETE"])
    def api_clear_history():
        try:
            agent = _get_agent()
            agent.clear_history()
            return jsonify({"status": "cleared"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ------------------------------------------------------------------
    # Memory API
    # ------------------------------------------------------------------

    @app.route("/api/memory")
    def api_memory_list():
        try:
            agent = _get_agent()
            entries = agent.memory.get_recent(30)
            return jsonify({
                "entries": [e.to_dict() for e in entries],
                "stats": agent.memory.stats(),
            })
        except Exception as e:
            return jsonify({"entries": [], "error": str(e)})

    @app.route("/api/memory/search")
    def api_memory_search():
        query = request.args.get("q", "")
        if not query:
            return jsonify({"results": [], "error": "Missing ?q= parameter"}), 400
        try:
            agent = _get_agent()
            results = agent.memory.search(query, limit=10)
            return jsonify({"results": [e.to_dict() for e in results]})
        except Exception as e:
            return jsonify({"results": [], "error": str(e)})

    @app.route("/api/memory/add", methods=["POST"])
    def api_memory_add():
        data = request.get_json(silent=True) or {}
        content = data.get("content", "").strip()
        title = data.get("title", content[:80])
        obs_type = data.get("obs_type", data.get("category", "note"))
        concepts = data.get("concepts", [])
        tags = data.get("tags", [])
        if not content:
            return jsonify({"error": "Missing content"}), 400
        try:
            agent = _get_agent()
            obs = agent.memory.add_observation(
                obs_type=obs_type, title=title, content=content,
                concepts=concepts, tags=tags, source="user",
            )
            if obs:
                return jsonify({"stored": obs.to_dict()})
            return jsonify({"stored": None, "note": "Deduplicated"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ------------------------------------------------------------------
    # Dependencies API
    # ------------------------------------------------------------------

    REQUIRED_PACKAGES = [
        ("psutil", "psutil"), ("cryptography", "cryptography"),
        ("flask", "flask"), ("requests", "requests"), ("rich", "rich"),
    ]
    OPTIONAL_PACKAGES = [
        ("anthropic", "anthropic"), ("openai", "openai"),
        ("google.auth", "google-auth"), ("huggingface_hub", "huggingface_hub"),
        ("pyautogui", "pyautogui"), ("pyperclip", "pyperclip"),
        ("pyaudio", "pyaudio"), ("assemblyai", "assemblyai"),
        ("elevenlabs", "elevenlabs"),
    ]

    @app.route("/api/dependencies")
    def api_dependencies():
        import importlib
        results = []
        for module, pkg in REQUIRED_PACKAGES + OPTIONAL_PACKAGES:
            is_required = any(pkg == p for _, p in REQUIRED_PACKAGES)
            try:
                mod = importlib.import_module(module)
                version = getattr(mod, "__version__", getattr(mod, "VERSION", "installed"))
                results.append({"package": pkg, "module": module, "installed": True,
                                "version": str(version), "required": is_required})
            except ImportError:
                results.append({"package": pkg, "module": module, "installed": False,
                                "version": None, "required": is_required})
        return jsonify({"packages": results})

    @app.route("/api/dependencies/update", methods=["POST"])
    def api_dependencies_update():
        import subprocess as _sp
        data = request.get_json(silent=True) or {}
        packages = data.get("packages", [])
        if not packages:
            # Update all installed
            packages = [pkg for _, pkg in REQUIRED_PACKAGES + OPTIONAL_PACKAGES]
        results = []
        for pkg in packages:
            try:
                r = _sp.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "-q", pkg],
                    capture_output=True, text=True, timeout=120,
                )
                results.append({"package": pkg, "success": r.returncode == 0,
                                "error": r.stderr.strip() if r.returncode else None})
            except Exception as e:
                results.append({"package": pkg, "success": False, "error": str(e)})
        return jsonify({"results": results})

    # ------------------------------------------------------------------
    # MCP servers (placeholder)
    # ------------------------------------------------------------------

    @app.route("/api/mcp/servers")
    def api_mcp_servers():
        return jsonify({"servers": [], "status": "MCP not yet connected"})

    # ------------------------------------------------------------------
    # Plugins (placeholder)
    # ------------------------------------------------------------------

    @app.route("/api/plugins")
    def api_plugins():
        return jsonify({"plugins": [], "status": "Plugin system not yet loaded"})

    # ------------------------------------------------------------------
    # Cowork sessions (placeholder)
    # ------------------------------------------------------------------

    @app.route("/api/cowork/sessions")
    def api_cowork_sessions():
        return jsonify({"sessions": [], "status": "No shared sessions"})

    return app
