"""WorldView — Geospatial Intelligence Dashboard.

CesiumJS-powered 3D globe with live satellite tracking, flight data,
seismic activity, and military-style visual filters (CRT, NVG, FLIR).
Inspired by Bilawal Sidhu's WorldView project.
"""

from __future__ import annotations

WORLDVIEW_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorldView — SDA Intelligence Dashboard</title>
<script>window.CESIUM_BASE_URL = 'https://cdn.jsdelivr.net/npm/cesium@1.119.0/Build/Cesium/';</script>
<script src="https://cdn.jsdelivr.net/npm/cesium@1.119.0/Build/Cesium/Cesium.js"></script>
<link href="https://cdn.jsdelivr.net/npm/cesium@1.119.0/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
  /* ===== RESET & BASE ===== */
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100%; height: 100%; overflow: hidden; background: #000; }
  body {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    color: #ffb000;
    font-size: 12px;
  }

  /* ===== CESIUM CONTAINER ===== */
  #cesiumContainer {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    z-index: 0;
  }
  #cesiumContainer canvas { display: block; }
  .cesium-viewer-bottom { display: none !important; }
  .cesium-widget-credits { display: none !important; }

  /* ===== VIEW MODE FILTERS ===== */
  body.mode-crt #cesiumContainer canvas {
    filter: sepia(100%) hue-rotate(70deg) saturate(400%) brightness(0.7) contrast(1.1);
  }
  body.mode-crt { --hud-color: #00ff41; --hud-dim: #007a1f; --hud-glow: rgba(0,255,65,0.3); }

  body.mode-nvg #cesiumContainer canvas {
    filter: sepia(100%) hue-rotate(70deg) saturate(500%) brightness(1.2) contrast(1.4);
  }
  body.mode-nvg { --hud-color: #39ff14; --hud-dim: #1a7a0a; --hud-glow: rgba(57,255,20,0.4); }

  body.mode-flir #cesiumContainer canvas {
    filter: saturate(150%) hue-rotate(190deg) brightness(0.85) contrast(1.4);
  }
  body.mode-flir { --hud-color: #ff6600; --hud-dim: #993d00; --hud-glow: rgba(255,102,0,0.3); }

  body.mode-normal { --hud-color: #ffb000; --hud-dim: #7a5500; --hud-glow: rgba(255,176,0,0.2); }

  /* ===== FILTER OVERLAYS ===== */
  .filter-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    pointer-events: none; z-index: 1;
  }
  #scanlines {
    display: none;
    background: repeating-linear-gradient(
      0deg, rgba(0,0,0,0.15) 0px, rgba(0,0,0,0.15) 1px, transparent 1px, transparent 3px
    );
    opacity: 0.6;
  }
  body.mode-crt #scanlines { display: block; }
  #vignette {
    display: none;
    background: radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.7) 100%);
  }
  body.mode-crt #vignette, body.mode-nvg #vignette { display: block; }
  #noise-overlay {
    display: none; opacity: 0.04;
    animation: noiseAnim 0.1s steps(8) infinite;
  }
  body.mode-nvg #noise-overlay { display: block; }
  @keyframes noiseAnim {
    0%   { background-position: 0 0; }
    50%  { background-position: 50% -50%; }
    100% { background-position: -50% 50%; }
  }
  #flir-overlay {
    display: none;
    background: radial-gradient(ellipse at center, transparent 40%, rgba(255,60,0,0.08) 100%);
  }
  body.mode-flir #flir-overlay { display: block; }

  /* ===== HUD ELEMENTS ===== */
  .hud {
    position: fixed; z-index: 10;
    color: var(--hud-color, #ffb000);
    text-shadow: 0 0 6px var(--hud-glow, rgba(255,176,0,0.2));
  }
  .hud-interactive { pointer-events: auto; }

  /* ===== TOP HEADER BAR ===== */
  #header {
    top: 0; left: 0; width: 100%; height: 42px;
    background: linear-gradient(180deg, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.4) 80%, transparent 100%);
    border-bottom: 1px solid rgba(255,176,0,0.15);
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 20px; font-size: 11px;
  }
  .header-left { display: flex; align-items: center; gap: 16px; }
  .logo {
    font-size: 16px; font-weight: 700; letter-spacing: 4px;
    background: linear-gradient(90deg, var(--hud-color, #ffb000), #ff6600);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .classification {
    font-size: 9px; letter-spacing: 2px; color: var(--hud-dim, #7a5500);
    border: 1px solid var(--hud-dim, #7a5500); padding: 2px 8px; text-transform: uppercase;
  }
  .header-center { font-size: 13px; font-weight: 600; letter-spacing: 2px; }
  .header-right { display: flex; gap: 16px; font-size: 10px; letter-spacing: 1px; }
  .header-right .stat { opacity: 0.8; }

  /* ===== LEFT CONTROL PANEL ===== */
  #controls {
    top: 52px; left: 10px; width: 220px;
    max-height: calc(100vh - 100px); overflow-y: auto;
    scrollbar-width: thin; scrollbar-color: rgba(255,176,0,0.3) transparent;
  }
  .panel {
    background: rgba(0,0,0,0.75); border: 1px solid rgba(255,176,0,0.12);
    border-radius: 4px; margin-bottom: 8px; padding: 10px 12px; backdrop-filter: blur(8px);
  }
  .panel h3 {
    font-size: 9px; letter-spacing: 3px; font-weight: 600; color: var(--hud-dim, #7a5500);
    margin-bottom: 8px; text-transform: uppercase;
    border-bottom: 1px solid rgba(255,176,0,0.08); padding-bottom: 4px;
  }
  .mode-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
  .mode-btn {
    background: rgba(255,176,0,0.05); border: 1px solid rgba(255,176,0,0.15);
    color: var(--hud-color, #ffb000); padding: 6px 0; font-size: 10px; font-weight: 600;
    letter-spacing: 1px; cursor: pointer; font-family: inherit; border-radius: 2px; transition: all 0.2s;
  }
  .mode-btn:hover { background: rgba(255,176,0,0.12); border-color: rgba(255,176,0,0.4); }
  .mode-btn.active {
    background: rgba(255,176,0,0.2); border-color: var(--hud-color, #ffb000);
    box-shadow: 0 0 8px var(--hud-glow, rgba(255,176,0,0.2));
  }
  .layer-toggle {
    display: flex; align-items: center; gap: 8px;
    padding: 5px 0; cursor: pointer; font-size: 11px; transition: opacity 0.2s;
  }
  .layer-toggle:hover { opacity: 1; }
  .layer-toggle input[type="checkbox"] { accent-color: var(--hud-color, #ffb000); width: 12px; height: 12px; }
  .layer-toggle .count { margin-left: auto; font-size: 9px; color: var(--hud-dim, #7a5500); }
  .preset-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 3px; }
  .preset-btn {
    background: rgba(255,176,0,0.04); border: 1px solid rgba(255,176,0,0.1);
    color: var(--hud-color, #ffb000); padding: 5px 4px; font-size: 9px;
    cursor: pointer; font-family: inherit; border-radius: 2px; transition: all 0.2s; letter-spacing: 0.5px;
  }
  .preset-btn:hover { background: rgba(255,176,0,0.12); border-color: rgba(255,176,0,0.4); }
  .btn-action {
    width: 100%;
    background: linear-gradient(135deg, rgba(255,176,0,0.15), rgba(255,100,0,0.15));
    border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000);
    padding: 8px; font-size: 10px; font-weight: 700; letter-spacing: 2px;
    cursor: pointer; font-family: inherit; border-radius: 2px; transition: all 0.2s;
  }
  .btn-action:hover {
    background: linear-gradient(135deg, rgba(255,176,0,0.25), rgba(255,100,0,0.25));
    box-shadow: 0 0 12px var(--hud-glow, rgba(255,176,0,0.2));
  }
  .conj-item { padding: 6px 0; border-bottom: 1px solid rgba(255,176,0,0.06); font-size: 10px; }
  .risk-tag {
    display: inline-block; padding: 1px 5px; border-radius: 2px;
    font-size: 8px; font-weight: 700; letter-spacing: 1px;
  }
  .risk-CRITICAL { background: rgba(255,0,0,0.25); color: #ff4040; border: 1px solid rgba(255,0,0,0.4); }
  .risk-HIGH { background: rgba(255,120,0,0.2); color: #ff8800; border: 1px solid rgba(255,120,0,0.3); }
  .risk-MODERATE { background: rgba(255,200,0,0.15); color: #ffc800; border: 1px solid rgba(255,200,0,0.3); }
  .risk-LOW { background: rgba(0,200,0,0.1); color: #00cc44; border: 1px solid rgba(0,200,0,0.2); }
  .risk-NEGLIGIBLE { background: rgba(100,100,100,0.1); color: #888; border: 1px solid rgba(100,100,100,0.2); }

  /* ===== RIGHT INFO PANEL ===== */
  #info-panel {
    top: 52px; right: 10px; width: 260px;
    background: rgba(0,0,0,0.8); border: 1px solid rgba(255,176,0,0.15);
    border-radius: 4px; padding: 12px; backdrop-filter: blur(8px);
    transition: transform 0.3s, opacity 0.3s;
  }
  #info-panel.hidden { transform: translateX(280px); opacity: 0; pointer-events: none; }
  #info-panel h3 {
    font-size: 9px; letter-spacing: 3px; color: var(--hud-dim, #7a5500);
    margin-bottom: 8px; text-transform: uppercase;
  }
  .info-row {
    display: flex; justify-content: space-between; padding: 3px 0;
    font-size: 10px; border-bottom: 1px solid rgba(255,176,0,0.05);
  }
  .info-label { color: var(--hud-dim, #7a5500); }
  .info-value { font-weight: 600; }
  .btn-dismiss {
    margin-top: 8px; width: 100%; background: none; border: 1px solid rgba(255,176,0,0.15);
    color: var(--hud-dim, #7a5500); padding: 5px; font-size: 9px;
    cursor: pointer; font-family: inherit; letter-spacing: 2px; border-radius: 2px;
  }
  .btn-dismiss:hover { border-color: var(--hud-color, #ffb000); color: var(--hud-color, #ffb000); }

  /* ===== BOTTOM STATUS BAR ===== */
  #status-bar {
    bottom: 0; left: 0; width: 100%; height: 28px;
    background: linear-gradient(0deg, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.4) 80%, transparent 100%);
    border-top: 1px solid rgba(255,176,0,0.1);
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 20px; font-size: 10px; letter-spacing: 1px; color: var(--hud-dim, #7a5500);
  }
  #status-bar span { min-width: 120px; }
  #status-bar .right { text-align: right; }

  /* ===== CROSSHAIR ===== */
  #crosshair {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 40px; height: 40px; z-index: 5; pointer-events: none; opacity: 0.3;
  }
  #crosshair .ch-h, #crosshair .ch-v { position: absolute; background: var(--hud-color, #ffb000); }
  #crosshair .ch-h { width: 100%; height: 1px; top: 50%; left: 0; }
  #crosshair .ch-v { width: 1px; height: 100%; top: 0; left: 50%; }

  /* ===== INTEL FEED ===== */
  #intel-feed {
    bottom: 38px; right: 10px; width: 280px; max-height: 200px; overflow-y: auto;
    scrollbar-width: thin; scrollbar-color: rgba(255,176,0,0.2) transparent;
  }
  .feed-item {
    background: rgba(0,0,0,0.7); border: 1px solid rgba(255,176,0,0.08);
    border-radius: 2px; padding: 6px 8px; margin-bottom: 3px;
    font-size: 9px; line-height: 1.4; backdrop-filter: blur(4px);
    animation: feedSlide 0.3s ease-out;
  }
  .feed-time { color: var(--hud-dim, #7a5500); font-size: 8px; }
  @keyframes feedSlide {
    from { transform: translateX(20px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }

  /* ===== LOADING OVERLAY ===== */
  #loading-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: #000; z-index: 9999;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    transition: opacity 0.8s;
  }
  #loading-overlay.fade-out { opacity: 0; pointer-events: none; }
  .loading-logo { font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #ffb000; margin-bottom: 20px; }
  .loading-bar { width: 200px; height: 2px; background: rgba(255,176,0,0.15); border-radius: 1px; overflow: hidden; }
  .loading-bar-fill { width: 0%; height: 100%; background: linear-gradient(90deg, #ffb000, #ff6600); transition: width 0.4s; }
  .loading-status { margin-top: 12px; font-size: 10px; color: #7a5500; letter-spacing: 2px; }

  @media (max-width: 768px) {
    #controls { width: 180px; font-size: 10px; }
    #info-panel { width: 220px; }
    #intel-feed { width: 220px; }
  }
</style>
</head>
<body class="mode-normal">

<!-- Loading Screen -->
<div id="loading-overlay">
  <div class="loading-logo">WORLDVIEW</div>
  <div class="loading-bar"><div class="loading-bar-fill" id="loading-fill"></div></div>
  <div class="loading-status" id="loading-status">INITIALIZING SYSTEMS...</div>
</div>

<!-- CesiumJS Globe -->
<div id="cesiumContainer"></div>

<!-- Filter Overlays -->
<div id="scanlines" class="filter-overlay"></div>
<div id="vignette" class="filter-overlay"></div>
<div id="noise-overlay" class="filter-overlay"></div>
<div id="flir-overlay" class="filter-overlay"></div>

<!-- Top Header -->
<header id="header" class="hud">
  <div class="header-left">
    <span class="logo">WORLDVIEW</span>
    <span class="classification">UNCLASSIFIED // SDA DEMONSTRATION</span>
  </div>
  <div class="header-center" id="utc-clock">--:--:-- UTC</div>
  <div class="header-right">
    <span class="stat" id="stat-sat">SAT: --</span>
    <span class="stat" id="stat-air">AIR: --</span>
    <span class="stat" id="stat-sei">SEI: --</span>
    <span class="stat" id="stat-conj">CONJ: --</span>
  </div>
</header>

<!-- Left Control Panel -->
<aside id="controls" class="hud hud-interactive">
  <div class="panel">
    <h3>View Mode</h3>
    <div class="mode-buttons">
      <button class="mode-btn active" data-mode="normal">NORMAL</button>
      <button class="mode-btn" data-mode="crt">CRT</button>
      <button class="mode-btn" data-mode="nvg">NVG</button>
      <button class="mode-btn" data-mode="flir">FLIR</button>
    </div>
  </div>
  <div class="panel">
    <h3>Data Layers</h3>
    <label class="layer-toggle">
      <input type="checkbox" id="layer-satellites" checked>
      <span>Satellites</span>
      <span class="count" id="count-sat">0</span>
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="layer-orbits" checked>
      <span>Orbit Paths</span>
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="layer-flights">
      <span>Aircraft</span>
      <span class="count" id="count-air">0</span>
    </label>
    <label class="layer-toggle">
      <input type="checkbox" id="layer-quakes" checked>
      <span>Earthquakes</span>
      <span class="count" id="count-sei">0</span>
    </label>
  </div>
  <div class="panel">
    <h3>Locations</h3>
    <div class="preset-grid">
      <button class="preset-btn" data-preset="global">GLOBAL</button>
      <button class="preset-btn" data-preset="dc">WASH DC</button>
      <button class="preset-btn" data-preset="london">LONDON</button>
      <button class="preset-btn" data-preset="moscow">MOSCOW</button>
      <button class="preset-btn" data-preset="beijing">BEIJING</button>
      <button class="preset-btn" data-preset="tokyo">TOKYO</button>
      <button class="preset-btn" data-preset="dubai">DUBAI</button>
      <button class="preset-btn" data-preset="sydney">SYDNEY</button>
      <button class="preset-btn" data-preset="cape">CAPE CANAV</button>
      <button class="preset-btn" data-preset="baikonur">BAIKONUR</button>
      <button class="preset-btn" data-preset="tehran">TEHRAN</button>
      <button class="preset-btn" data-preset="pyongyang">PYONGYANG</button>
    </div>
  </div>
  <div class="panel">
    <h3>Conjunction Analysis</h3>
    <div style="display:flex;gap:6px;margin-bottom:6px;">
      <div style="flex:1">
        <div style="font-size:8px;color:var(--hud-dim);letter-spacing:1px;margin-bottom:2px;">HOURS</div>
        <input type="number" id="conj-hours" value="24" min="1" max="168"
          style="width:100%;background:rgba(255,176,0,0.05);border:1px solid rgba(255,176,0,0.15);
          color:var(--hud-color);padding:4px;font-family:inherit;font-size:10px;border-radius:2px;">
      </div>
      <div style="flex:1">
        <div style="font-size:8px;color:var(--hud-dim);letter-spacing:1px;margin-bottom:2px;">THRESH KM</div>
        <input type="number" id="conj-threshold" value="10" min="0.1" max="100" step="0.1"
          style="width:100%;background:rgba(255,176,0,0.05);border:1px solid rgba(255,176,0,0.15);
          color:var(--hud-color);padding:4px;font-family:inherit;font-size:10px;border-radius:2px;">
      </div>
    </div>
    <button class="btn-action" id="btn-analyze">ANALYZE CONJUNCTIONS</button>
    <div id="conj-results" style="margin-top:8px;max-height:200px;overflow-y:auto;"></div>
  </div>
  <div class="panel" style="text-align:center;">
    <a href="/" style="color:var(--hud-dim);font-size:9px;letter-spacing:2px;text-decoration:none;">
      OPEN SDA DASHBOARD
    </a>
  </div>
</aside>

<!-- Right Info Panel -->
<aside id="info-panel" class="hud hud-interactive hidden">
  <h3>TRACKED ENTITY</h3>
  <div id="entity-info"></div>
  <button class="btn-dismiss" id="btn-dismiss">DISMISS</button>
</aside>

<!-- Intel Feed -->
<div id="intel-feed" class="hud"></div>

<!-- Bottom Status Bar -->
<footer id="status-bar" class="hud">
  <span id="coords">LAT: --.---- LON: --.----</span>
  <span id="cam-alt">ALT: -- km</span>
  <span class="right" id="fps-display">FPS: --</span>
</footer>

<!-- Crosshair -->
<div id="crosshair">
  <div class="ch-h"></div>
  <div class="ch-v"></div>
</div>

<script>
// ============================================================
// WORLDVIEW — SDA Intelligence Dashboard
// ============================================================

var API = '';
var REFRESH_SATS_MS = 5000;
var REFRESH_FLIGHTS_MS = 15000;
var REFRESH_QUAKES_MS = 60000;

// ===== CAMERA PRESETS =====
var PRESETS = {
  global:    { lon: 0,       lat: 20,     alt: 20000000, name: 'Global View' },
  dc:        { lon: -77.04,  lat: 38.90,  alt: 80000,    name: 'Washington DC' },
  london:    { lon: -0.12,   lat: 51.51,  alt: 80000,    name: 'London' },
  moscow:    { lon: 37.62,   lat: 55.75,  alt: 80000,    name: 'Moscow' },
  beijing:   { lon: 116.39,  lat: 39.91,  alt: 80000,    name: 'Beijing' },
  tokyo:     { lon: 139.69,  lat: 35.69,  alt: 80000,    name: 'Tokyo' },
  dubai:     { lon: 55.27,   lat: 25.20,  alt: 80000,    name: 'Dubai' },
  sydney:    { lon: 151.21,  lat: -33.87, alt: 80000,    name: 'Sydney' },
  cape:      { lon: -80.60,  lat: 28.39,  alt: 50000,    name: 'Cape Canaveral' },
  baikonur:  { lon: 63.34,   lat: 45.62,  alt: 50000,    name: 'Baikonur Cosmodrome' },
  tehran:    { lon: 51.39,   lat: 35.69,  alt: 80000,    name: 'Tehran' },
  pyongyang: { lon: 125.75,  lat: 39.02,  alt: 80000,    name: 'Pyongyang' },
};

// ===== STATE =====
var viewer = null;
var satEntities = {};
var flightEntities = {};
var quakeEntities = {};
var orbitPolylines = {};
var selectedEntity = null;
var layerState = { satellites: true, orbits: true, flights: false, quakes: true };

// ===== DOM HELPERS =====
function makeInfoRow(label, value) {
  var row = document.createElement('div');
  row.className = 'info-row';
  var lbl = document.createElement('span');
  lbl.className = 'info-label';
  lbl.textContent = label;
  var val = document.createElement('span');
  val.className = 'info-value';
  val.textContent = value;
  row.appendChild(lbl);
  row.appendChild(val);
  return row;
}

function makeConjItem(e) {
  var item = document.createElement('div');
  item.className = 'conj-item';
  var tag = document.createElement('span');
  tag.className = 'risk-tag risk-' + e.risk;
  tag.textContent = e.risk;
  item.appendChild(tag);
  item.appendChild(document.createTextNode(' '));
  var primary = document.createElement('span');
  primary.style.color = 'var(--hud-color)';
  primary.textContent = e.primary_name || String(e.primary);
  item.appendChild(primary);
  item.appendChild(document.createTextNode(' / '));
  var secondary = document.createElement('span');
  secondary.style.color = 'var(--hud-color)';
  secondary.textContent = e.secondary_name || String(e.secondary);
  item.appendChild(secondary);
  var br = document.createElement('br');
  item.appendChild(br);
  var detail = document.createElement('span');
  detail.style.color = 'var(--hud-dim)';
  detail.textContent = 'Miss: ' + e.miss_distance_km.toFixed(3) + ' km | Vel: ' + e.relative_velocity_km_s.toFixed(2) + ' km/s';
  item.appendChild(detail);
  return item;
}

// ===== LOADING =====
function setLoadingProgress(pct, msg) {
  var fill = document.getElementById('loading-fill');
  var status = document.getElementById('loading-status');
  if (fill) fill.style.width = pct + '%';
  if (status) status.textContent = msg;
}

function hideLoading() {
  var overlay = document.getElementById('loading-overlay');
  if (overlay) {
    overlay.classList.add('fade-out');
    setTimeout(function() { overlay.remove(); }, 1000);
  }
}

// ===== CESIUM INIT =====
function initCesium() {
  setLoadingProgress(10, 'LOADING GLOBE ENGINE...');

  Cesium.Ion.defaultAccessToken = undefined;

  viewer = new Cesium.Viewer('cesiumContainer', {
    animation: false,
    timeline: false,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    infoBox: false,
    selectionIndicator: false,
    creditContainer: document.createElement('div'),
    requestRenderMode: false,
    maximumRenderTimeChange: Infinity,
    targetFrameRate: 60,
    orderIndependentTranslucency: false,
    contextOptions: {
      webgl: { alpha: false, antialias: true, preserveDrawingBuffer: false }
    },
  });

  // Dark basemap
  viewer.imageryLayers.removeAll();
  viewer.imageryLayers.addImageryProvider(
    new Cesium.UrlTemplateImageryProvider({
      url: 'https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
      maximumLevel: 18,
      credit: new Cesium.Credit('CartoDB'),
    })
  );

  // Globe styling
  var scene = viewer.scene;
  var globe = scene.globe;
  scene.backgroundColor = Cesium.Color.BLACK;
  globe.baseColor = Cesium.Color.fromCssColorString('#0a0a0a');
  globe.enableLighting = false;
  globe.showGroundAtmosphere = false;

  scene.skyAtmosphere.show = true;
  scene.skyAtmosphere.hueShift = -0.05;
  scene.skyAtmosphere.saturationShift = -0.4;
  scene.skyAtmosphere.brightnessShift = -0.3;
  scene.sun.show = false;
  scene.moon.show = false;

  // Initial camera
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(0, 20, 20000000),
    orientation: { heading: 0, pitch: Cesium.Math.toRadians(-90), roll: 0 }
  });

  // Click handler
  var handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  handler.setInputAction(function(click) {
    var picked = scene.pick(click.position);
    if (Cesium.defined(picked) && Cesium.defined(picked.id)) {
      selectEntity(picked.id);
    } else {
      deselectEntity();
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  setLoadingProgress(30, 'GLOBE ENGINE READY');
}

// ===== INTEL FEED =====
function addIntelItem(msg) {
  var feed = document.getElementById('intel-feed');
  var item = document.createElement('div');
  item.className = 'feed-item';
  var ts = document.createElement('span');
  ts.className = 'feed-time';
  ts.textContent = new Date().toISOString().slice(11, 19) + ' UTC';
  item.appendChild(ts);
  item.appendChild(document.createTextNode(' ' + msg));
  feed.prepend(item);
  while (feed.children.length > 20) feed.lastChild.remove();
}

// ===== SATELLITE LAYER =====
async function loadSatellites() {
  if (!layerState.satellites) return;
  try {
    var resp = await fetch(API + '/api/satellite-positions');
    if (!resp.ok) return;
    var data = await resp.json();
    var sats = data.satellites || [];

    document.getElementById('count-sat').textContent = sats.length;
    document.getElementById('stat-sat').textContent = 'SAT: ' + sats.length;

    var currentIds = new Set();
    for (var i = 0; i < sats.length; i++) {
      var sat = sats[i];
      currentIds.add(String(sat.norad_id));
      var id = 'sat-' + sat.norad_id;

      if (satEntities[id]) {
        satEntities[id].position = Cesium.Cartesian3.fromDegrees(sat.lon, sat.lat, sat.alt_km * 1000);
      } else {
        satEntities[id] = viewer.entities.add({
          id: id,
          name: sat.name,
          position: Cesium.Cartesian3.fromDegrees(sat.lon, sat.lat, sat.alt_km * 1000),
          point: {
            pixelSize: 5, color: Cesium.Color.CYAN,
            outlineColor: Cesium.Color.fromAlpha(Cesium.Color.CYAN, 0.3), outlineWidth: 2,
          },
          label: {
            text: sat.name, font: '10px JetBrains Mono',
            fillColor: Cesium.Color.CYAN, outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(10, -10),
            scaleByDistance: new Cesium.NearFarScalar(1e6, 1, 1e8, 0.4),
            translucencyByDistance: new Cesium.NearFarScalar(1e7, 1, 5e7, 0.5),
            showBackground: false,
          },
          properties: {
            type: 'satellite', norad_id: sat.norad_id,
            alt_km: sat.alt_km, velocity: sat.velocity_km_s,
          }
        });
      }
    }
    // Remove stale
    for (var eid in satEntities) {
      if (!currentIds.has(eid.replace('sat-', ''))) {
        viewer.entities.remove(satEntities[eid]);
        delete satEntities[eid];
      }
    }
  } catch (e) { /* silent retry on next cycle */ }
}

// ===== ORBIT PATHS =====
async function loadOrbitPaths() {
  if (!layerState.orbits) {
    for (var oid in orbitPolylines) { viewer.entities.remove(orbitPolylines[oid]); }
    orbitPolylines = {};
    return;
  }
  try {
    var resp = await fetch(API + '/api/satellite-orbits');
    if (!resp.ok) return;
    var data = await resp.json();
    for (var oid2 in orbitPolylines) { viewer.entities.remove(orbitPolylines[oid2]); }
    orbitPolylines = {};
    for (var j = 0; j < data.orbits.length; j++) {
      var orbit = data.orbits[j];
      if (!orbit.path || orbit.path.length < 2) continue;
      var positions = [];
      for (var k = 0; k < orbit.path.length; k++) {
        var p = orbit.path[k];
        positions.push(Cesium.Cartesian3.fromDegrees(p.lon, p.lat, p.alt_km * 1000));
      }
      var lineId = 'orbit-' + orbit.norad_id;
      orbitPolylines[lineId] = viewer.entities.add({
        id: lineId,
        polyline: {
          positions: positions, width: 1,
          material: new Cesium.ColorMaterialProperty(Cesium.Color.CYAN.withAlpha(0.25)),
        }
      });
    }
  } catch (e) { /* silent */ }
}

// ===== FLIGHT LAYER =====
async function loadFlights() {
  if (!layerState.flights) return;
  try {
    var resp = await fetch(API + '/api/flights');
    if (!resp.ok) return;
    var data = await resp.json();
    var flights = data.flights || [];
    document.getElementById('count-air').textContent = flights.length;
    document.getElementById('stat-air').textContent = 'AIR: ' + flights.length;

    var currentIds = new Set();
    for (var i = 0; i < flights.length; i++) {
      var f = flights[i];
      if (!f.lon || !f.lat) continue;
      currentIds.add(f.icao24);
      var id = 'flight-' + f.icao24;
      var alt = (f.alt_m || 10000);
      var isMil = !!f.is_military;
      var ptColor = isMil ? Cesium.Color.ORANGE : Cesium.Color.fromCssColorString('#44aaff');

      if (flightEntities[id]) {
        flightEntities[id].position = Cesium.Cartesian3.fromDegrees(f.lon, f.lat, alt);
      } else {
        flightEntities[id] = viewer.entities.add({
          id: id, name: (f.callsign || f.icao24).trim(),
          position: Cesium.Cartesian3.fromDegrees(f.lon, f.lat, alt),
          point: { pixelSize: 3, color: ptColor, outlineWidth: 0 },
          label: {
            text: (f.callsign || '').trim(), font: '9px JetBrains Mono',
            fillColor: ptColor, outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(8, -5),
            scaleByDistance: new Cesium.NearFarScalar(5e4, 1, 2e6, 0),
            translucencyByDistance: new Cesium.NearFarScalar(1e5, 1, 5e6, 0),
            showBackground: false,
          },
          properties: {
            type: isMil ? 'military' : 'aircraft', icao24: f.icao24,
            origin_country: f.origin_country, velocity: f.velocity, heading: f.heading,
          }
        });
      }
    }
    for (var fid in flightEntities) {
      if (!currentIds.has(fid.replace('flight-', ''))) {
        viewer.entities.remove(flightEntities[fid]);
        delete flightEntities[fid];
      }
    }
  } catch (e) { /* silent */ }
}

// ===== EARTHQUAKE LAYER =====
async function loadEarthquakes() {
  if (!layerState.quakes) return;
  try {
    var resp = await fetch(API + '/api/earthquakes');
    if (!resp.ok) return;
    var data = await resp.json();
    var quakes = data.earthquakes || [];
    document.getElementById('count-sei').textContent = quakes.length;
    document.getElementById('stat-sei').textContent = 'SEI: ' + quakes.length;

    for (var qid in quakeEntities) { viewer.entities.remove(quakeEntities[qid]); }
    quakeEntities = {};

    for (var i = 0; i < quakes.length; i++) {
      var q = quakes[i];
      var qeid = 'quake-' + q.id;
      var mag = q.magnitude;
      var size = Math.max(6, mag * 4);
      var color = mag >= 6 ? Cesium.Color.RED :
                  mag >= 4.5 ? Cesium.Color.ORANGE :
                  mag >= 3 ? Cesium.Color.YELLOW :
                  Cesium.Color.fromCssColorString('#888');

      quakeEntities[qeid] = viewer.entities.add({
        id: qeid, name: q.title,
        position: Cesium.Cartesian3.fromDegrees(q.lon, q.lat, 0),
        point: { pixelSize: size, color: color.withAlpha(0.7), outlineColor: color, outlineWidth: 1 },
        label: {
          text: 'M' + mag.toFixed(1), font: '9px JetBrains Mono',
          fillColor: color, outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(size + 4, 0),
          scaleByDistance: new Cesium.NearFarScalar(1e5, 1, 1e7, 0.3),
          translucencyByDistance: new Cesium.NearFarScalar(1e6, 1, 2e7, 0),
        },
        properties: { type: 'earthquake', magnitude: mag, place: q.place, time: q.time }
      });
    }
  } catch (e) { /* silent */ }
}

// ===== CONJUNCTION ANALYSIS =====
async function runConjunctionAnalysis() {
  var btn = document.getElementById('btn-analyze');
  var results = document.getElementById('conj-results');
  btn.textContent = 'ANALYZING...';
  btn.disabled = true;
  try {
    var hours = parseFloat(document.getElementById('conj-hours').value) || 24;
    var threshold = parseFloat(document.getElementById('conj-threshold').value) || 10;
    var resp = await fetch(API + '/conjunctions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hours: hours, threshold_km: threshold })
    });
    var events = await resp.json();
    document.getElementById('stat-conj').textContent = 'CONJ: ' + events.length;
    results.replaceChildren();
    if (events.length === 0) {
      var noResult = document.createElement('div');
      noResult.style.cssText = 'font-size:10px;color:var(--hud-dim);padding:6px 0;';
      noResult.textContent = 'No conjunctions detected';
      results.appendChild(noResult);
    } else {
      for (var i = 0; i < events.length; i++) {
        results.appendChild(makeConjItem(events[i]));
      }
      addIntelItem('Conjunction analysis: ' + events.length + ' events detected');
    }
  } catch (e) {
    var errDiv = document.createElement('div');
    errDiv.style.cssText = 'color:#ff4040;font-size:10px;';
    errDiv.textContent = 'Analysis failed';
    results.replaceChildren(errDiv);
  }
  btn.textContent = 'ANALYZE CONJUNCTIONS';
  btn.disabled = false;
}

// ===== ENTITY SELECTION =====
function selectEntity(entity) {
  selectedEntity = entity;
  var panel = document.getElementById('info-panel');
  var info = document.getElementById('entity-info');
  panel.classList.remove('hidden');
  info.replaceChildren();

  var props = entity.properties;
  var type = (props && props.type) ? props.type.getValue() : 'unknown';

  info.appendChild(makeInfoRow('NAME', entity.name || 'Unknown'));
  info.appendChild(makeInfoRow('TYPE', type.toUpperCase()));

  if (type === 'satellite' && props) {
    info.appendChild(makeInfoRow('NORAD ID', String(props.norad_id ? props.norad_id.getValue() : '')));
    info.appendChild(makeInfoRow('ALTITUDE', (props.alt_km ? props.alt_km.getValue().toFixed(1) : '0') + ' km'));
    info.appendChild(makeInfoRow('VELOCITY', (props.velocity ? props.velocity.getValue().toFixed(3) : '0') + ' km/s'));
  } else if ((type === 'aircraft' || type === 'military') && props) {
    info.appendChild(makeInfoRow('COUNTRY', props.origin_country ? props.origin_country.getValue() : ''));
    var vel = props.velocity ? props.velocity.getValue() : 0;
    info.appendChild(makeInfoRow('SPEED', vel ? vel.toFixed(0) + ' m/s' : '--'));
    var hdg = props.heading ? props.heading.getValue() : 0;
    info.appendChild(makeInfoRow('HEADING', hdg ? hdg.toFixed(0) + '\u00B0' : '--'));
  } else if (type === 'earthquake' && props) {
    info.appendChild(makeInfoRow('MAGNITUDE', 'M' + (props.magnitude ? props.magnitude.getValue().toFixed(1) : '?')));
    info.appendChild(makeInfoRow('LOCATION', props.place ? props.place.getValue() : ''));
    info.appendChild(makeInfoRow('TIME', props.time ? props.time.getValue() : ''));
  }

  if (type === 'satellite') { viewer.trackedEntity = entity; }
}

function deselectEntity() {
  selectedEntity = null;
  document.getElementById('info-panel').classList.add('hidden');
  viewer.trackedEntity = undefined;
}

// ===== VIEW MODE SWITCHING =====
function setViewMode(mode) {
  document.body.className = 'mode-' + mode;
  document.querySelectorAll('.mode-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  addIntelItem('View mode: ' + mode.toUpperCase());
}

// ===== CAMERA PRESETS =====
function flyToPreset(key) {
  var p = PRESETS[key];
  if (!p) return;
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(p.lon, p.lat, p.alt),
    orientation: { heading: 0, pitch: Cesium.Math.toRadians(p.alt > 1000000 ? -90 : -45), roll: 0 },
    duration: 2.0,
  });
  addIntelItem('Camera: ' + p.name);
}

// ===== LAYER TOGGLES =====
function toggleLayer(layer, enabled) {
  layerState[layer] = enabled;
  var id;
  if (layer === 'satellites') {
    for (id in satEntities) { satEntities[id].show = enabled; }
    if (enabled) loadSatellites();
  }
  if (layer === 'orbits') {
    if (enabled) { loadOrbitPaths(); }
    else { for (id in orbitPolylines) { viewer.entities.remove(orbitPolylines[id]); } orbitPolylines = {}; }
  }
  if (layer === 'flights') {
    for (id in flightEntities) { flightEntities[id].show = enabled; }
    if (enabled) { loadFlights(); addIntelItem('Aircraft layer enabled'); }
  }
  if (layer === 'quakes') {
    for (id in quakeEntities) { quakeEntities[id].show = enabled; }
    if (enabled) loadEarthquakes();
  }
}

// ===== HUD UPDATES =====
function updateClock() {
  document.getElementById('utc-clock').textContent =
    new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

function updateStatusBar() {
  if (!viewer) return;
  var carto = viewer.camera.positionCartographic;
  if (carto) {
    var lat = Cesium.Math.toDegrees(carto.latitude);
    var lon = Cesium.Math.toDegrees(carto.longitude);
    var alt = carto.height / 1000;
    document.getElementById('coords').textContent = 'LAT: ' + lat.toFixed(4) + ' LON: ' + lon.toFixed(4);
    document.getElementById('cam-alt').textContent = 'ALT: ' + (alt > 1000 ? (alt/1000).toFixed(1) + ' Mm' : alt.toFixed(0) + ' km');
  }
}

var frameCount = 0;
var lastFpsTime = performance.now();
function updateFPS() {
  frameCount++;
  var now = performance.now();
  if (now - lastFpsTime >= 1000) {
    document.getElementById('fps-display').textContent = 'FPS: ' + frameCount;
    frameCount = 0;
    lastFpsTime = now;
  }
}

// ===== EVENT LISTENERS =====
function setupEventListeners() {
  document.querySelectorAll('.mode-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { setViewMode(btn.dataset.mode); });
  });
  document.querySelectorAll('.preset-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { flyToPreset(btn.dataset.preset); });
  });
  document.getElementById('layer-satellites').addEventListener('change', function() { toggleLayer('satellites', this.checked); });
  document.getElementById('layer-orbits').addEventListener('change', function() { toggleLayer('orbits', this.checked); });
  document.getElementById('layer-flights').addEventListener('change', function() { toggleLayer('flights', this.checked); });
  document.getElementById('layer-quakes').addEventListener('change', function() { toggleLayer('quakes', this.checked); });
  document.getElementById('btn-analyze').addEventListener('click', runConjunctionAnalysis);
  document.getElementById('btn-dismiss').addEventListener('click', deselectEntity);

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    switch(e.key) {
      case '1': setViewMode('normal'); break;
      case '2': setViewMode('crt'); break;
      case '3': setViewMode('nvg'); break;
      case '4': setViewMode('flir'); break;
      case 'Escape': deselectEntity(); break;
      case 'g': flyToPreset('global'); break;
    }
  });
}

// ===== MAIN INIT =====
async function init() {
  try {
    initCesium();
    setupEventListeners();
    setLoadingProgress(50, 'LOADING SATELLITE DATA...');
    await loadSatellites();
    setLoadingProgress(70, 'LOADING ORBIT PATHS...');
    await loadOrbitPaths();
    setLoadingProgress(85, 'LOADING SEISMIC DATA...');
    await loadEarthquakes();
    setLoadingProgress(95, 'STARTING DATA FEEDS...');
    setInterval(loadSatellites, REFRESH_SATS_MS);
    setInterval(loadOrbitPaths, 30000);
    setInterval(loadEarthquakes, REFRESH_QUAKES_MS);
    setInterval(function() { if (layerState.flights) loadFlights(); }, REFRESH_FLIGHTS_MS);
    setInterval(updateClock, 1000);
    updateClock();
    viewer.scene.postRender.addEventListener(function() { updateStatusBar(); updateFPS(); });
    setLoadingProgress(100, 'SYSTEMS ONLINE');
    addIntelItem('WorldView initialized \u2014 all systems operational');
    addIntelItem('Keys: 1-4 view modes, G global view, ESC deselect');
    setTimeout(hideLoading, 600);
  } catch (e) {
    setLoadingProgress(0, 'INIT FAILED: ' + e.message);
  }
}

init();
</script>
</body>
</html>
"""
