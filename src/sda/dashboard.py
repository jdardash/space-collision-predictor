"""Embedded web dashboard for the SDA collision predictor."""

from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SDA Collision Predictor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@300;400;600;700&display=swap');

  :root {
    --bg-primary: #0a0a1a;
    --bg-secondary: #0f1029;
    --bg-card: #141432;
    --bg-card-hover: #1a1a42;
    --border: #1e1e4a;
    --text-primary: #e0e0ff;
    --text-secondary: #8888bb;
    --accent-blue: #00d4ff;
    --accent-green: #00ff88;
    --accent-red: #ff4466;
    --accent-yellow: #ffcc00;
    --accent-orange: #ff8844;
    --accent-purple: #aa66ff;
    --glow-blue: rgba(0, 212, 255, 0.15);
    --glow-red: rgba(255, 68, 102, 0.15);
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Inter', system-ui, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    overflow-x: hidden;
  }

  body::before {
    content: '';
    position: fixed;
    top: 0; left: 0; width: 100%; height: 100%;
    background:
      radial-gradient(1px 1px at 20% 30%, rgba(255,255,255,0.3), transparent),
      radial-gradient(1px 1px at 40% 70%, rgba(255,255,255,0.2), transparent),
      radial-gradient(1px 1px at 60% 20%, rgba(255,255,255,0.3), transparent),
      radial-gradient(1px 1px at 80% 50%, rgba(255,255,255,0.15), transparent),
      radial-gradient(1.5px 1.5px at 10% 80%, rgba(0,212,255,0.4), transparent),
      radial-gradient(1.5px 1.5px at 90% 10%, rgba(0,255,136,0.3), transparent),
      radial-gradient(1px 1px at 50% 50%, rgba(255,255,255,0.2), transparent),
      radial-gradient(1px 1px at 70% 85%, rgba(255,255,255,0.25), transparent),
      radial-gradient(1px 1px at 30% 15%, rgba(255,255,255,0.2), transparent),
      radial-gradient(1px 1px at 85% 65%, rgba(170,102,255,0.3), transparent);
    pointer-events: none;
    z-index: 0;
  }

  .app { position: relative; z-index: 1; }

  header {
    background: linear-gradient(180deg, var(--bg-secondary) 0%, transparent 100%);
    border-bottom: 1px solid var(--border);
    padding: 1.25rem 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .logo { display: flex; align-items: center; gap: 0.75rem; }

  .logo-icon {
    width: 36px; height: 36px;
    background: conic-gradient(from 0deg, var(--accent-blue), var(--accent-purple), var(--accent-blue));
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
    animation: spin 20s linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  .logo h1 {
    font-size: 1.2rem; font-weight: 700;
    background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }

  .logo .subtitle {
    font-size: 0.7rem; color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 2px;
  }

  .header-status { display: flex; align-items: center; gap: 1.5rem; }

  .status-indicator {
    display: flex; align-items: center; gap: 0.5rem;
    font-size: 0.8rem; color: var(--text-secondary);
  }

  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent-green);
    box-shadow: 0 0 8px var(--accent-green);
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

  main {
    max-width: 1400px; margin: 0 auto;
    padding: 1.5rem 2rem; display: grid; gap: 1.5rem;
  }

  .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; }

  .stat-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 1.25rem; transition: all 0.3s ease;
  }

  .stat-card:hover {
    background: var(--bg-card-hover); border-color: var(--accent-blue);
    box-shadow: 0 0 20px var(--glow-blue);
  }

  .stat-label {
    font-size: 0.75rem; color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 0.5rem;
  }

  .stat-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 2rem; font-weight: 700;
  }

  .stat-value.blue { color: var(--accent-blue); }
  .stat-value.green { color: var(--accent-green); }
  .stat-value.red { color: var(--accent-red); }
  .stat-value.yellow { color: var(--accent-yellow); }

  .stat-detail { font-size: 0.7rem; color: var(--text-secondary); margin-top: 0.25rem; }

  .content-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }

  .card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden;
  }

  .card-header {
    padding: 1rem 1.25rem; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }

  .card-title {
    font-size: 0.85rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 1px;
  }

  .card-body { padding: 1rem 1.25rem; }

  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }

  th {
    text-align: left; padding: 0.6rem 0.75rem;
    color: var(--text-secondary); font-weight: 600;
    text-transform: uppercase; font-size: 0.7rem;
    letter-spacing: 1px; border-bottom: 1px solid var(--border);
  }

  td {
    padding: 0.6rem 0.75rem;
    border-bottom: 1px solid rgba(30, 30, 74, 0.5);
    font-family: 'JetBrains Mono', monospace; font-size: 0.75rem;
  }

  tr:hover td { background: rgba(0, 212, 255, 0.03); }

  .risk-badge {
    display: inline-block; padding: 0.2rem 0.6rem;
    border-radius: 4px; font-size: 0.65rem;
    font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  }

  .risk-CRITICAL {
    background: rgba(255, 68, 102, 0.2); color: var(--accent-red);
    border: 1px solid rgba(255, 68, 102, 0.4);
    animation: pulse 1.5s ease-in-out infinite;
  }

  .risk-HIGH {
    background: rgba(255, 136, 68, 0.2); color: var(--accent-orange);
    border: 1px solid rgba(255, 136, 68, 0.4);
  }

  .risk-MODERATE {
    background: rgba(255, 204, 0, 0.2); color: var(--accent-yellow);
    border: 1px solid rgba(255, 204, 0, 0.4);
  }

  .risk-LOW {
    background: rgba(0, 255, 136, 0.15); color: var(--accent-green);
    border: 1px solid rgba(0, 255, 136, 0.3);
  }

  .risk-NEGLIGIBLE {
    background: rgba(136, 136, 187, 0.15); color: var(--text-secondary);
    border: 1px solid rgba(136, 136, 187, 0.3);
  }

  .btn {
    padding: 0.5rem 1rem; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg-card);
    color: var(--text-primary); font-size: 0.75rem;
    cursor: pointer; transition: all 0.2s; font-family: 'Inter', sans-serif;
  }

  .btn:hover { background: var(--bg-card-hover); border-color: var(--accent-blue); }

  .btn-primary {
    background: linear-gradient(135deg, rgba(0,212,255,0.2), rgba(170,102,255,0.2));
    border-color: var(--accent-blue);
  }

  .btn-primary:hover {
    background: linear-gradient(135deg, rgba(0,212,255,0.35), rgba(170,102,255,0.35));
    box-shadow: 0 0 15px var(--glow-blue);
  }

  .btn-danger { border-color: var(--accent-red); color: var(--accent-red); }

  .btn-danger:hover {
    background: rgba(255, 68, 102, 0.15);
    box-shadow: 0 0 15px var(--glow-red);
  }

  textarea, input, select {
    background: var(--bg-primary); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text-primary);
    padding: 0.5rem 0.75rem; font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem; width: 100%; resize: vertical;
  }

  textarea:focus, input:focus {
    outline: none; border-color: var(--accent-blue);
    box-shadow: 0 0 10px var(--glow-blue);
  }

  .viz-frame {
    width: 100%; height: 550px; border: none;
    border-radius: 0 0 12px 12px;
  }

  .loading {
    display: flex; align-items: center; justify-content: center;
    padding: 2rem; color: var(--text-secondary); gap: 0.5rem;
  }

  .spinner {
    width: 16px; height: 16px;
    border: 2px solid var(--border); border-top-color: var(--accent-blue);
    border-radius: 50%; animation: spin 0.8s linear infinite;
  }

  .empty-state {
    text-align: center; padding: 2rem;
    color: var(--text-secondary); font-size: 0.8rem;
  }

  .modal-overlay {
    display: none; position: fixed; top: 0; left: 0;
    width: 100%; height: 100%; background: rgba(0,0,0,0.7);
    z-index: 1000; align-items: center; justify-content: center;
  }

  .modal-overlay.active { display: flex; }

  .modal {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; width: 600px; max-width: 90vw;
    max-height: 80vh; overflow-y: auto;
  }

  .modal-header {
    padding: 1rem 1.25rem; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }

  .modal-body { padding: 1.25rem; }

  .modal-footer {
    padding: 1rem 1.25rem; border-top: 1px solid var(--border);
    display: flex; justify-content: flex-end; gap: 0.5rem;
  }

  .form-group { margin-bottom: 1rem; }

  .form-label {
    display: block; font-size: 0.75rem; color: var(--text-secondary);
    margin-bottom: 0.4rem; text-transform: uppercase; letter-spacing: 1px;
  }

  .toast-container {
    position: fixed; top: 1rem; right: 1rem; z-index: 2000;
    display: flex; flex-direction: column; gap: 0.5rem;
  }

  .toast {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 8px; padding: 0.75rem 1rem; font-size: 0.8rem;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5); animation: slideIn 0.3s ease;
    max-width: 350px;
  }

  .toast.success { border-color: var(--accent-green); }
  .toast.error { border-color: var(--accent-red); }

  @keyframes slideIn {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }

  .controls-row {
    display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap;
  }

  .controls-row label {
    font-size: 0.7rem; color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 1px;
  }

  .controls-row input { width: 70px; }

  @media (max-width: 900px) {
    .stats-row { grid-template-columns: repeat(2, 1fr); }
    .content-grid { grid-template-columns: 1fr; }
    header { flex-direction: column; gap: 1rem; }
  }
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="logo">
      <div class="logo-icon">&#x1F6F0;</div>
      <div>
        <h1>SDA Collision Predictor</h1>
        <div class="subtitle">Space-Domain Awareness Engine</div>
      </div>
    </div>
    <div class="header-status">
      <div class="status-indicator">
        <div class="status-dot"></div>
        <span id="status-text">System Online</span>
      </div>
      <div class="status-indicator" style="color: var(--text-primary);">
        <span id="clock"></span>
      </div>
    </div>
  </header>

  <main>
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-label">Tracked Satellites</div>
        <div class="stat-value blue" id="stat-satellites">--</div>
        <div class="stat-detail">Active catalog objects</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Conjunctions Found</div>
        <div class="stat-value yellow" id="stat-conjunctions">--</div>
        <div class="stat-detail">Last analysis window</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Highest Risk</div>
        <div class="stat-value green" id="stat-risk">NONE</div>
        <div class="stat-detail">Current threat level</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Analysis Window</div>
        <div class="stat-value blue" id="stat-window">24h</div>
        <div class="stat-detail">Propagation horizon</div>
      </div>
    </div>

    <div class="content-grid">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Satellite Catalog</span>
          <div style="display:flex; gap:0.5rem;">
            <button class="btn btn-primary" onclick="openTLEModal()">+ Ingest TLE</button>
            <button class="btn" onclick="loadSatellites()">Refresh</button>
          </div>
        </div>
        <div class="card-body" style="padding:0; max-height:350px; overflow-y:auto;">
          <table>
            <thead>
              <tr><th>NORAD ID</th><th>Name</th><th>Epoch</th><th></th></tr>
            </thead>
            <tbody id="sat-table-body">
              <tr><td colspan="4" class="empty-state">Loading...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-title">Conjunction Events</span>
          <div class="controls-row">
            <label>Hours:</label>
            <input type="number" id="analysis-hours" value="24" min="1" max="168" style="width:60px;">
            <label>Threshold:</label>
            <input type="number" id="analysis-threshold" value="10" min="0.1" max="100" step="0.1" style="width:60px;">
            <span style="font-size:0.65rem;color:var(--text-secondary)">km</span>
            <button class="btn btn-primary" onclick="runAnalysis()">Analyze</button>
          </div>
        </div>
        <div class="card-body" style="padding:0; max-height:350px; overflow-y:auto;">
          <table>
            <thead>
              <tr><th>Risk</th><th>Primary</th><th>Secondary</th><th>Miss (km)</th><th>Vel (km/s)</th><th>TCA (UTC)</th></tr>
            </thead>
            <tbody id="conj-table-body">
              <tr><td colspan="6" class="empty-state">Run analysis to detect conjunctions</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span class="card-title">3D Orbit Visualization</span>
        <div style="display:flex; gap:0.5rem;">
          <button class="btn" onclick="loadVisualization()">Refresh View</button>
          <button class="btn" onclick="openVizFullscreen()">Open Fullscreen</button>
        </div>
      </div>
      <div id="viz-container">
        <iframe id="viz-frame" class="viz-frame" srcdoc="<div style='display:flex;align-items:center;justify-content:center;height:100%;background:#0a0a2e;color:#8888bb;font-family:Inter,sans-serif;font-size:0.9rem;'>Load satellites and run analysis to see 3D visualization</div>"></iframe>
      </div>
    </div>
  </main>
</div>

<div class="modal-overlay" id="tle-modal">
  <div class="modal">
    <div class="modal-header">
      <span class="card-title">Ingest TLE Data</span>
      <button class="btn" onclick="closeTLEModal()">&times;</button>
    </div>
    <div class="modal-body">
      <div class="form-group">
        <label class="form-label">Paste TLE Text (2-line or 3-line format)</label>
        <textarea id="tle-input" rows="10" placeholder="ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"></textarea>
      </div>
      <div style="font-size:0.7rem; color:var(--text-secondary);">
        Supports standard 2-line and 3-line TLE formats. Multiple TLE sets can be pasted at once.
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeTLEModal()">Cancel</button>
      <button class="btn btn-primary" onclick="ingestTLE()">Ingest</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toasts"></div>

<script>
  const API = '';

  function updateClock() {
    const el = document.getElementById('clock');
    el.textContent = new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
  }
  setInterval(updateClock, 1000);
  updateClock();

  function showToast(msg, type) {
    type = type || 'success';
    const container = document.getElementById('toasts');
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(function() { toast.remove(); }, 4000);
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function buildSatRow(s) {
    var tr = document.createElement('tr');
    var td1 = document.createElement('td');
    td1.textContent = s.norad_id;
    var td2 = document.createElement('td');
    td2.style.color = 'var(--accent-blue)';
    td2.textContent = s.name;
    var td3 = document.createElement('td');
    td3.textContent = new Date(s.epoch).toISOString().slice(0,16).replace('T',' ');
    var td4 = document.createElement('td');
    var btn = document.createElement('button');
    btn.className = 'btn btn-danger';
    btn.style.cssText = 'padding:0.2rem 0.5rem;font-size:0.65rem;';
    btn.textContent = '\u00D7';
    btn.addEventListener('click', function() { deleteSat(s.norad_id); });
    td4.appendChild(btn);
    tr.appendChild(td1);
    tr.appendChild(td2);
    tr.appendChild(td3);
    tr.appendChild(td4);
    return tr;
  }

  function buildConjRow(e) {
    var tr = document.createElement('tr');
    var td1 = document.createElement('td');
    var badge = document.createElement('span');
    badge.className = 'risk-badge risk-' + e.risk;
    badge.textContent = e.risk;
    td1.appendChild(badge);
    var td2 = document.createElement('td');
    td2.style.color = 'var(--accent-blue)';
    td2.textContent = e.primary_name || e.primary;
    var td3 = document.createElement('td');
    td3.style.color = 'var(--accent-blue)';
    td3.textContent = e.secondary_name || e.secondary;
    var td4 = document.createElement('td');
    td4.textContent = e.miss_distance_km.toFixed(3);
    var td5 = document.createElement('td');
    td5.textContent = e.relative_velocity_km_s.toFixed(2);
    var td6 = document.createElement('td');
    td6.textContent = new Date(e.tca).toISOString().slice(0,19).replace('T',' ');
    tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
    tr.appendChild(td4); tr.appendChild(td5); tr.appendChild(td6);
    return tr;
  }

  function setTableMessage(tbodyId, cols, msg) {
    var tbody = document.getElementById(tbodyId);
    tbody.replaceChildren();
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = cols;
    td.className = 'empty-state';
    td.textContent = msg;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function setTableLoading(tbodyId, cols, msg) {
    var tbody = document.getElementById(tbodyId);
    tbody.replaceChildren();
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = cols;
    var div = document.createElement('div');
    div.className = 'loading';
    var spinner = document.createElement('div');
    spinner.className = 'spinner';
    div.appendChild(spinner);
    div.appendChild(document.createTextNode(msg));
    td.appendChild(div);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  async function loadSatellites() {
    try {
      var resp = await fetch(API + '/satellites');
      var sats = await resp.json();
      document.getElementById('stat-satellites').textContent = sats.length;
      var tbody = document.getElementById('sat-table-body');
      tbody.replaceChildren();

      if (sats.length === 0) {
        setTableMessage('sat-table-body', 4, 'No satellites tracked. Ingest TLE data to begin.');
        return;
      }

      sats.forEach(function(s) { tbody.appendChild(buildSatRow(s)); });
    } catch (e) {
      showToast('Failed to load satellites: ' + e.message, 'error');
    }
  }

  async function deleteSat(id) {
    try {
      await fetch(API + '/satellites/' + id, { method: 'DELETE' });
      showToast('Satellite ' + id + ' removed');
      loadSatellites();
    } catch (e) {
      showToast('Failed to delete satellite', 'error');
    }
  }

  async function runAnalysis() {
    var hours = parseFloat(document.getElementById('analysis-hours').value) || 24;
    var threshold = parseFloat(document.getElementById('analysis-threshold').value) || 10;
    document.getElementById('stat-window').textContent = hours + 'h';

    setTableLoading('conj-table-body', 6, 'Running conjunction analysis...');

    try {
      var resp = await fetch(API + '/conjunctions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hours: hours, threshold_km: threshold })
      });
      var events = await resp.json();
      document.getElementById('stat-conjunctions').textContent = events.length;

      var riskColors = {
        CRITICAL: 'var(--accent-red)', HIGH: 'var(--accent-orange)',
        MODERATE: 'var(--accent-yellow)', LOW: 'var(--accent-green)',
        NEGLIGIBLE: 'var(--text-secondary)'
      };

      if (events.length === 0) {
        document.getElementById('stat-risk').textContent = 'NONE';
        document.getElementById('stat-risk').style.color = 'var(--accent-green)';
        setTableMessage('conj-table-body', 6, 'No conjunctions detected in the analysis window');
      } else {
        document.getElementById('stat-risk').textContent = events[0].risk;
        document.getElementById('stat-risk').style.color = riskColors[events[0].risk] || 'var(--text-primary)';

        var tbody = document.getElementById('conj-table-body');
        tbody.replaceChildren();
        events.forEach(function(e) { tbody.appendChild(buildConjRow(e)); });
      }

      loadVisualization();
    } catch (e) {
      setTableMessage('conj-table-body', 6, 'Analysis failed: ' + e.message);
      showToast('Analysis failed: ' + e.message, 'error');
    }
  }

  function loadVisualization() {
    var hours = document.getElementById('analysis-hours').value || 24;
    var threshold = document.getElementById('analysis-threshold').value || 10;
    document.getElementById('viz-frame').src =
      API + '/conjunctions/visualize?hours=' + hours + '&threshold_km=' + threshold;
  }

  function openVizFullscreen() {
    var hours = document.getElementById('analysis-hours').value || 24;
    var threshold = document.getElementById('analysis-threshold').value || 10;
    window.open('/conjunctions/visualize?hours=' + hours + '&threshold_km=' + threshold, '_blank');
  }

  function openTLEModal() {
    document.getElementById('tle-modal').classList.add('active');
  }

  function closeTLEModal() {
    document.getElementById('tle-modal').classList.remove('active');
  }

  async function ingestTLE() {
    var text = document.getElementById('tle-input').value.trim();
    if (!text) { showToast('Please paste TLE data', 'error'); return; }

    try {
      var resp = await fetch(API + '/tle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tle_text: text })
      });
      var data = await resp.json();
      showToast('Ingested ' + data.ingested + ' satellite(s). Total tracked: ' + data.total_tracked);
      closeTLEModal();
      document.getElementById('tle-input').value = '';
      loadSatellites();
    } catch (e) {
      showToast('Ingest failed: ' + e.message, 'error');
    }
  }

  loadSatellites();
</script>
</body>
</html>
"""
