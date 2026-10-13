// Conjunction actions for the WorldView globe: three server analyses on the open
// conjunction (compare Pc methods, avoidance options, a hypothetical breakup preview).
//
// Nothing is computed here. POST /pc/compare, POST /maneuver and POST /breakup/simulate
// do the work; this file builds the request bodies from the event the page already has,
// shows what comes back, and states every assumption it had to make to build the request.
//
// Donor (reuse first, MIT): cesabici-bit/satguard, https://github.com/cesabici-bit/satguard,
// commit 07a86ce, web/src/components/ConjunctionBrowser.tsx (Pc bands above 1e-4 and above
// 1e-6, Pc as toExponential with "N/A" for a missing value, the truncate helper, the
// compact badge-plus-numbers row) and ObjectInspector.tsx (Pc to two digits). MIT License,
// Copyright (c) 2026 Genius Lab. Only those presentation rules were taken, as logic, not
// as code; the bands are restyled in this page's risk colours. Everything else is ours.
//
// The pure helpers at the top need no DOM and are exported for the Node tests in
// tests/test_globe_conjactions.py. init() is the only part that touches the page.

(function (global) {
  'use strict';

  var MAX_FRAGMENTS = 300;
  var CLOUD_INTERVAL_MS = 500;
  var REQUEST_TIMEOUT_MS = 45000;
  var MAX_MESSAGE_CHARS = 150;

  // 10 km is where classify_risk stops calling an approach LOW and calls it NEGLIGIBLE, so
  // asking for it means "move this out of the risk table". It is shown on screen.
  var MANEUVER_TARGET_KM = 10;

  // The catalog holds no masses, so the breakup preview assumes these and says so.
  var PARENT_MASS_KG = 1000;
  var PROJECTILE_MASS_KG = 10;

  var PC_HIGH = 1e-4;
  var PC_MED = 1e-6;

  var STYLE_ID = 'conjactions-style';
  var CLOUD_COLOR = '#ff8800';
  var CLOUD_PIXEL_SIZE = 3;

  var LEAD = {
    compare: 'Pc comparison',
    maneuver: 'Avoidance options',
    breakup: 'Breakup preview'
  };
  var RUNNING = {
    compare: 'Comparing Pc methods...',
    maneuver: 'Planning avoidance options...',
    breakup: 'Simulating the breakup...'
  };
  var PATH = {
    compare: '/pc/compare',
    maneuver: '/maneuver',
    breakup: '/breakup/simulate'
  };
  var METHODS = [
    { key: 'foster', label: 'Foster' },
    { key: 'chan', label: 'Chan' },
    { key: 'alfano', label: 'Alfano' },
    { key: 'monte_carlo', label: 'Monte Carlo' }
  ];

  // ---------------------------------------------------------------------
  // Pure helpers
  // ---------------------------------------------------------------------

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

  function toId(v) {
    var n = v;
    if (typeof n === 'string' && /^\s*\d+\s*$/.test(n)) n = parseInt(n, 10);
    return (isNum(n) && n > 0 && Math.floor(n) === n) ? n : null;
  }

  function fixed(n, digits) { return isNum(n) ? n.toFixed(digits) : '?'; }

  // 5.20 becomes "5.2" and 0.02 stays "0.02", without float noise.
  function tidy(n) { return isNum(n) ? String(Math.round(n * 1000) / 1000) : '?'; }

  function formatPc(p) {
    if (!isNum(p)) return 'N/A';
    return p === 0 ? '0' : p.toExponential(2);
  }

  function pcBand(p) {
    if (!isNum(p)) return '';
    if (p > PC_HIGH) return 'HIGH';
    return p > PC_MED ? 'MED' : 'LOW';
  }

  function truncate(s, max) {
    var t = String(s);
    return t.length > max ? t.slice(0, max - 1) + '...' : t;
  }

  // Smallest delta-V first; equal delta-V keeps the server's order. Returns a copy.
  function sortOptions(options) {
    return (options || []).map(function (o, i) { return { o: o, i: i }; })
      .sort(function (a, b) {
        var d = a.o.delta_v_m_s - b.o.delta_v_m_s;
        return d !== 0 && !isNaN(d) ? d : a.i - b.i;
      })
      .map(function (w) { return w.o; });
  }

  function oneLine(message) {
    var t = String(message).replace(/\s+/g, ' ').replace(/^ | $/g, '');
    if (t.length > MAX_MESSAGE_CHARS) t = t.slice(0, MAX_MESSAGE_CHARS - 3) + '...';
    return /[.!?]$/.test(t) ? t : t + '.';
  }

  function sentence(message) {
    var t = oneLine(message);
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  // One short sentence for a failed call. `status` is the HTTP status, or 0 when no
  // response arrived. FastAPI sends 4xx as { detail: "text" } or, for a 422 from the
  // request model, a list of { loc: [...], msg: "..." }.
  function describeError(status, body) {
    if (!status) return 'Could not reach the server.';
    if (status === 429) {
      var wait = body ? body.retry_after_seconds : null;
      return isNum(wait)
        ? 'Too many requests, try again in ' + Math.ceil(wait) + ' s.'
        : 'Too many requests, try again shortly.';
    }
    var detail = body ? body.detail : null;
    if (status >= 400 && status < 500) {
      if (typeof detail === 'string' && detail) return oneLine(detail);
      if (Array.isArray(detail) && detail[0] && typeof detail[0].msg === 'string') {
        var loc = Array.isArray(detail[0].loc) ? detail[0].loc.slice() : [];
        if (loc[0] === 'body') loc.shift();
        var where = loc.length ? loc.join('.') : 'the request';
        return oneLine('The server rejected ' + where + ': ' + detail[0].msg);
      }
      return 'The server refused the request (HTTP ' + status + ').';
    }
    return 'The server failed to answer (HTTP ' + status + ').';
  }

  function describeTimeout(ms) {
    return 'The server did not answer within ' + Math.round(ms / 1000) + ' s.';
  }

  var NOT_EXPECTED = 'The server answer was not in the form the panel expected.';

  // ---------------------------------------------------------------------
  // Request bodies
  // ---------------------------------------------------------------------

  function fail(error) { return { ok: false, error: error }; }

  function ids(ev) {
    if (!isObj(ev)) return null;
    var p = toId(ev.primary != null ? ev.primary : ev.primary_norad_id);
    var s = toId(ev.secondary != null ? ev.secondary : ev.secondary_norad_id);
    return p && s ? { primary: p, secondary: s } : null;
  }

  function tcaOf(ev) {
    return isObj(ev) && typeof ev.tca === 'string' && ev.tca ? ev.tca : null;
  }

  function bplaneOf(ev) {
    var b = ev.bplane;
    var x = isObj(b) ? b.miss_x_km : ev.bplane_x_km;
    var y = isObj(b) ? b.miss_y_km : ev.bplane_y_km;
    return isNum(x) && isNum(y) ? [x, y] : null;
  }

  function diag(sigma) {
    var s2 = sigma * sigma;
    return [[s2, 0, 0], [0, s2, 0], [0, 0, s2]];
  }

  // /pc/compare wants 3-vectors, and the page's event has only the B-plane miss and the
  // relative speed. With a round covariance Pc depends on nothing else, so the encounter
  // is rebuilt as miss = [x, y, 0] across a velocity along +z. The covariance is the
  // event's own TLE-age estimate; with none, the fields are left out and the server's
  // defaults apply and are reported back. The hard-body radius is never sent.
  function buildCompareBody(ev) {
    if (!isObj(ev)) return fail('There is no conjunction open.');
    var speed = ev.relative_velocity_km_s;
    if (!isNum(speed) || speed <= 0) {
      return fail('This event has no usable relative speed, so Pc cannot be compared.');
    }
    var plane = bplaneOf(ev);
    var miss;
    var geometry;
    if (plane) {
      miss = [plane[0], plane[1], 0];
      geometry = 'bplane';
    } else if (isNum(ev.miss_distance_km)) {
      miss = [ev.miss_distance_km, 0, 0];
      geometry = 'miss-distance';
    } else {
      return fail('This event has no miss distance, so Pc cannot be compared.');
    }
    var body = { miss_vector_km: miss, rel_velocity_km_s: [0, 0, speed] };
    var cov = isObj(ev.covariance) ? ev.covariance : null;
    var sigmas = null;
    if (cov && isNum(cov.primary_sigma_km) && isNum(cov.secondary_sigma_km) &&
        cov.primary_sigma_km > 0 && cov.secondary_sigma_km > 0) {
      sigmas = [cov.primary_sigma_km, cov.secondary_sigma_km];
      body.cov_primary_km2 = diag(sigmas[0]);
      body.cov_secondary_km2 = diag(sigmas[1]);
    }
    body.include_monte_carlo = true;
    return {
      ok: true,
      body: body,
      geometry: geometry,
      covariance: sigmas ? 'event' : 'server-default',
      sigmas: sigmas
    };
  }

  function buildManeuverBody(ev) {
    var pair = ids(ev);
    if (!pair) return fail('This event has no catalog numbers, so no maneuver can be planned.');
    var tca = tcaOf(ev);
    if (!tca) return fail('This event has no closest-approach time, so no maneuver can be planned.');
    return {
      ok: true,
      body: {
        primary_norad_id: pair.primary,
        secondary_norad_id: pair.secondary,
        tca: tca,
        target_miss_km: MANEUVER_TARGET_KM
      }
    };
  }

  function buildBreakupBody(ev) {
    var pair = ids(ev);
    if (!pair) return fail('This event has no catalog numbers, so no breakup can be simulated.');
    var tca = tcaOf(ev);
    if (!tca) return fail('This event has no closest-approach time, so no breakup can be simulated.');
    var speed = isObj(ev) ? ev.relative_velocity_km_s : null;
    if (!isNum(speed) || speed <= 0) {
      return fail('This event has no usable relative speed, so no breakup can be simulated.');
    }
    return {
      ok: true,
      body: {
        norad_id: pair.primary,
        epoch: tca,
        event_type: 'collision',
        parent_type: 'spacecraft',
        parent_mass_kg: PARENT_MASS_KG,
        projectile_mass_kg: PROJECTILE_MASS_KG,
        relative_speed_km_s: speed,
        include_tles: true,
        max_tle_objects: MAX_FRAGMENTS
      }
    };
  }

  // ---------------------------------------------------------------------
  // Answers to text
  // ---------------------------------------------------------------------

  function nameOf(ev, which) {
    var name = ev[which + '_name'];
    if (typeof name === 'string' && name) return truncate(name, 40);
    var id = ev[which] != null ? ev[which] : ev[which + '_norad_id'];
    return 'NORAD ' + id;
  }

  // The one line that says what the Pc below rests on. Geometry and speed are read back
  // from the server's own echo, so a mismatch with what was sent would show here.
  function compareAssumption(built, data) {
    var parts = [];
    if (built.covariance === 'event' && built.sigmas) {
      parts.push('round covariance from TLE age (primary ' + fixed(built.sigmas[0], 3) +
        ' km, secondary ' + fixed(built.sigmas[1], 3) + ' km, 1-sigma)');
    } else {
      var notes = Array.isArray(data.assumptions) && data.assumptions.length
        ? data.assumptions.join('; ') : 'none reported';
      parts.push('this event carries no covariance, so the server defaults apply (' +
        notes + ')');
    }
    parts.push('hard-body radius ' + tidy(data.hard_body_radius_km) + ' km (server default)');
    var miss = fixed(isNum(data.miss_distance_km) ? data.miss_distance_km : null, 3);
    var speed = fixed(isNum(data.relative_speed_km_s) ? data.relative_speed_km_s : null, 3);
    parts.push('encounter rebuilt from the ' +
      (built.geometry === 'bplane' ? 'B-plane miss ' : 'miss distance ') + miss +
      ' km and relative speed ' + speed + ' km/s');
    return 'Assumed: ' + parts.join(', ') + '.';
  }

  function maneuverAssumption(ev, data, options) {
    var parts = ['impulsive burn on ' + nameOf(ev, 'primary') + ' (the primary)',
      'target miss ' + tidy(isNum(data.target_miss_km) ? data.target_miss_km : MANEUVER_TARGET_KM) +
      ' km'];
    var leads = options.map(function (o) { return o.lead_time_hours; }).filter(isNum);
    if (leads.length) {
      var lo = Math.min.apply(null, leads);
      var hi = Math.max.apply(null, leads);
      parts.push(lo === hi ? 'burn ' + tidy(lo) + ' h before closest approach'
        : 'burns ' + tidy(lo) + ' to ' + tidy(hi) + ' h before closest approach');
    }
    return 'Assumed: ' + parts.join(', ') + '.';
  }

  function breakupAssumption(ev, body) {
    return 'Hypothetical: this models a collision between ' + nameOf(ev, 'primary') + ' and ' +
      nameOf(ev, 'secondary') + ' at the time of closest approach, not an observed event. ' +
      'Assumed masses ' + tidy(body.parent_mass_kg) + ' kg and ' +
      tidy(body.projectile_mass_kg) + ' kg, relative speed ' +
      fixed(body.relative_speed_km_s, 3) + ' km/s from this event.';
  }

  function breakupNotes(data) {
    var s = isObj(data.summary) ? data.summary : {};
    var lines = [];
    lines.push('Catastrophic: ' + (data.catastrophic ? 'yes' : 'no') + ' (specific energy ' +
      Math.round(data.specific_energy_j_per_g) + ' J/g, effective mass ' +
      Math.round(data.effective_mass_kg) + ' kg).');
    lines.push('About ' + Math.round(data.total_fragments_modelled) +
      ' fragments above 1 cm, ' + Math.round(data.n_above_10cm) +
      ' above 10 cm in the full model.');
    if (isNum(s.median_delta_v_m_s) && isNum(s.max_characteristic_length_m)) {
      lines.push('Median fragment delta-V ' + Math.round(s.median_delta_v_m_s) +
        ' m/s, largest piece ' + fixed(s.max_characteristic_length_m, 2) + ' m.');
    }
    return lines;
  }

  // Name line (optional), line 1, line 2, repeated. Anything that does not form a pair
  // of lines starting "1 " and "2 " is ignored.
  function parseTleSets(text) {
    var lines = String(text || '').split(/\r?\n/)
      .map(function (l) { return l.replace(/\s+$/, ''); })
      .filter(function (l) { return l !== ''; });
    var sets = [];
    for (var i = 0; i < lines.length - 1; i++) {
      if (/^1 /.test(lines[i]) && /^2 /.test(lines[i + 1])) {
        sets.push({ line1: lines[i], line2: lines[i + 1] });
        i += 1;
      }
    }
    return sets;
  }

  function validAnswer(action, data) {
    if (!isObj(data)) return false;
    if (action === 'compare') return isObj(data.probabilities);
    if (action === 'maneuver') return Array.isArray(data.options);
    return typeof data.catastrophic === 'boolean' && isNum(data.total_fragments_modelled);
  }

  // ---------------------------------------------------------------------
  // Panel (DOM)
  // ---------------------------------------------------------------------

  var CSS = [
    '.ca-root { margin-top: 10px; font-size: 10px; }',
    '.ca-block { padding: 8px 0; border-top: 1px solid rgba(255,176,0,0.12); }',
    '.ca-head { display: flex; gap: 6px; }',
    '.ca-btn { flex: 1; background: rgba(255,176,0,0.08); color: var(--hud-color, #ffb000);',
    '  border: 1px solid rgba(255,176,0,0.3); border-radius: 2px; padding: 5px 8px;',
    '  font-size: 10px; font-family: inherit; letter-spacing: 1px; cursor: pointer; }',
    '.ca-btn:hover { background: rgba(255,176,0,0.18); }',
    '.ca-btn:disabled { opacity: 0.45; cursor: default; }',
    '.ca-clear { flex: 0 0 auto; }',
    '.ca-status, .ca-assume, .ca-note, .ca-cloudline, .ca-warn { margin-top: 6px;',
    '  line-height: 1.4; }',
    '.ca-status:empty { display: none; }',
    '.ca-error { color: #ff8800; }',
    '.ca-assume, .ca-cloudline { color: var(--hud-dim, #7a5500); }',
    '.ca-warn { color: #ffc800; }',
    '.ca-row, .ca-opt { display: flex; align-items: baseline; gap: 6px; margin-top: 4px; }',
    '.ca-k { flex: 0 0 74px; letter-spacing: 1px; color: var(--hud-dim, #7a5500); }',
    '.ca-v { flex: 1; font-weight: 600; }',
    '.ca-band { flex: 0 0 34px; font-size: 9px; text-align: right; }',
    '.ca-band-high { color: #ff8800; }',
    '.ca-band-med { color: #ffc800; }',
    '.ca-band-low { color: #00cc44; }',
    '.ca-opt-dir { flex: 0 0 74px; }',
    '.ca-opt-dv { flex: 0 0 76px; font-weight: 600; }',
    '.ca-opt-lead { flex: 1; color: var(--hud-dim, #7a5500); }',
    '.ca-opt-miss { flex: 0 0 52px; text-align: right; }',
    '.ca-best .ca-opt-dv { color: #00cc44; }',
    '.ca-static { color: var(--hud-dim, #7a5500); }'
  ].join('\n');

  function el(tag, className, content) {
    var node = global.document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = content;
    return node;
  }

  function injectStyle() {
    var doc = global.document;
    if (doc.getElementById(STYLE_ID)) return;
    var style = el('style', '', CSS);
    style.id = STYLE_ID;
    doc.head.appendChild(style);
  }

  var active = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount) return null;
      if (active) active.destroy();
      active = createPanel(ctx);
      return active;
    } catch (e) {
      active = null;
      return null;
    }
  }

  function createPanel(ctx) {
    var doc = global.document;
    var api = typeof ctx.api === 'string' ? ctx.api : '';
    var timeoutMs = (isNum(ctx.timeoutMs) && ctx.timeoutMs > 0) ? ctx.timeoutMs
      : REQUEST_TIMEOUT_MS;
    var root = el('div', 'ca-root');
    var inst = { destroy: destroy };
    var gone = false;

    injectStyle();

    if (ctx.isStatic) {
      root.appendChild(el('div', 'ca-static',
        'These analyses need the live server and are not available in the static demo.'));
      ctx.mount.appendChild(root);
      inst.destroy = function () {
        gone = true;
        if (root.parentNode) root.parentNode.removeChild(root);
        if (active === inst) active = null;
      };
      return inst;
    }

    var parts = {};
    var runs = { compare: null, maneuver: null, breakup: null };
    var cloud = null;
    var cloudLine = null;
    var opened = null;

    function currentEvent() {
      if (typeof ctx.getConjunction === 'function') return ctx.getConjunction() || null;
      return opened;
    }

    function eventKey(ev) {
      var pair = ids(ev);
      return pair ? pair.primary + '|' + pair.secondary + '|' + String(ev.tca) : '';
    }

    function makeBlock(action, label) {
      var block = el('div', 'ca-block');
      block.setAttribute('data-block', action);
      var head = el('div', 'ca-head');
      var button = el('button', 'ca-btn', label);
      button.setAttribute('data-action', action);
      button.addEventListener('click', function () { run(action); });
      head.appendChild(button);
      var p = { block: block, button: button, status: el('div', 'ca-status'),
        result: el('div', 'ca-result') };
      if (action === 'breakup') {
        p.clear = el('button', 'ca-btn ca-clear', 'Clear');
        p.clear.setAttribute('data-action', 'clear');
        p.clear.disabled = true;
        p.clear.addEventListener('click', function () { clearCloud('Cloud cleared from the globe.'); });
        head.appendChild(p.clear);
      }
      block.appendChild(head);
      block.appendChild(p.status);
      block.appendChild(p.result);
      parts[action] = p;
      return block;
    }

    function say(action, text, isError) {
      var node = parts[action].status;
      node.className = 'ca-status' + (isError ? ' ca-error' : '');
      node.textContent = text;
    }

    function add(action, className, text) {
      var node = el('div', className, text);
      parts[action].result.appendChild(node);
      return node;
    }

    function clearResult(action) {
      parts[action].result.textContent = '';
      if (action === 'breakup') cloudLine = null;
      say(action, '', false);
    }

    // ----- the fragment cloud -----

    function refreshCloud() {
      if (!cloud) return;
      try {
        var when = ctx.simNow();
        var gmst = satellite.gstime(when);
        for (var i = 0; i < cloud.recs.length; i++) {
          var point = cloud.points[i];
          var pv;
          try { pv = satellite.propagate(cloud.recs[i], when); } catch (e) { pv = null; }
          if (!pv || !pv.position) { point.show = false; continue; }
          var gd = satellite.eciToGeodetic(pv.position, gmst);
          point.position = Cesium.Cartesian3.fromRadians(gd.longitude, gd.latitude, gd.height * 1000);
          point.show = true;
        }
        var scene = ctx.viewer && ctx.viewer.scene;
        if (scene && typeof scene.requestRender === 'function') scene.requestRender();
      } catch (e) {
        // A clock or library hiccup must not stop the timer or reach the page.
      }
    }

    function clearCloud(message) {
      if (cloud) {
        clearInterval(cloud.timer);
        try { ctx.viewer.scene.primitives.remove(cloud.collection); } catch (e) { /* gone */ }
        cloud = null;
      }
      parts.breakup.clear.disabled = true;
      if (message && cloudLine) cloudLine.textContent = message;
    }

    // Returns the number drawn, or -1 when the page has no satellite.js or Cesium.
    function startCloud(sets) {
      if (typeof satellite === 'undefined' || !satellite ||
          typeof satellite.twoline2satrec !== 'function' ||
          typeof Cesium === 'undefined' || !Cesium || !Cesium.PointPrimitiveCollection ||
          !ctx.viewer || !ctx.viewer.scene || !ctx.viewer.scene.primitives) {
        return -1;
      }
      var recs = [];
      for (var i = 0; i < sets.length && recs.length < MAX_FRAGMENTS; i++) {
        var rec = null;
        try { rec = satellite.twoline2satrec(sets[i].line1, sets[i].line2); } catch (e) { rec = null; }
        if (rec) recs.push(rec);
      }
      if (!recs.length) return 0;
      var collection = new Cesium.PointPrimitiveCollection();
      var color = Cesium.Color.fromCssColorString(CLOUD_COLOR);
      var points = recs.map(function () {
        return collection.add({ pixelSize: CLOUD_PIXEL_SIZE, color: color, show: false });
      });
      ctx.viewer.scene.primitives.add(collection);
      cloud = { collection: collection, recs: recs, points: points, timer: null };
      refreshCloud();
      cloud.timer = setInterval(refreshCloud, CLOUD_INTERVAL_MS);
      parts.breakup.clear.disabled = false;
      return recs.length;
    }

    // ----- rendering -----

    function renderCompare(run, data) {
      add('compare', 'ca-assume', compareAssumption(run.built, data));
      METHODS.forEach(function (m) {
        var p = data.probabilities[m.key];
        var band = pcBand(p);
        var row = add('compare', 'ca-row');
        row.appendChild(el('span', 'ca-k', m.label));
        row.appendChild(el('span', 'ca-v', formatPc(p)));
        row.appendChild(el('span', 'ca-band' + (band ? ' ca-band-' + band.toLowerCase() : ''),
          band));
      });
      if (isNum(data.method_spread)) {
        add('compare', 'ca-note', 'Spread between methods: ' + formatPc(data.method_spread) + '.');
      }
      var usage = isObj(data.usage) ? data.usage : null;
      var warnings = usage && Array.isArray(usage.warnings) ? usage.warnings : [];
      warnings.forEach(function (w) {
        var node = add('compare', 'ca-warn', sentence(isObj(w) ? w.message : w));
        if (isObj(w) && typeof w.code === 'string') node.setAttribute('title', w.code);
      });
      if (usage && usage.ok === true && !warnings.length) {
        add('compare', 'ca-note', 'Usage checks: all passed.');
      }
    }

    function renderManeuver(run, data) {
      var options = sortOptions(data.options);
      if (options.length) add('maneuver', 'ca-assume', maneuverAssumption(run.event, data, options));
      if (options.length) {
        add('maneuver', 'ca-note', options.length + (options.length === 1 ? ' option' : ' options') +
          ', smallest delta-V first. Current miss ' + fixed(data.current_miss_km, 2) + ' km.');
      }
      options.forEach(function (o, i) {
        var row = add('maneuver', 'ca-opt' + (i === 0 ? ' ca-best' : ''));
        row.appendChild(el('span', 'ca-opt-dir', String(o.direction)));
        row.appendChild(el('span', 'ca-opt-dv',
          (isNum(o.delta_v_m_s) ? String(Math.round(o.delta_v_m_s * 1e4) / 1e4) : '?') + ' m/s'));
        row.appendChild(el('span', 'ca-opt-lead', tidy(o.lead_time_hours) + ' h before TCA'));
        row.appendChild(el('span', 'ca-opt-miss', fixed(o.new_miss_distance_km, 1) + ' km'));
      });
      if (data.warning) add('maneuver', 'ca-note', oneLine(data.warning));
    }

    function renderBreakup(run, data) {
      add('breakup', 'ca-assume', breakupAssumption(run.event, run.built.body));
      breakupNotes(data).forEach(function (line) { add('breakup', 'ca-note', line); });
      var sets = parseTleSets(data.tle_text);
      var drawn;
      try { drawn = startCloud(sets); } catch (e) { drawn = -2; }
      if (drawn === -1) {
        add('breakup', 'ca-note', 'The satellite library is not loaded, so the fragments cannot be drawn.');
      } else if (drawn === -2) {
        add('breakup', 'ca-note', 'The fragments could not be drawn on the globe.');
      } else if (drawn === 0) {
        cloudLine = add('breakup', 'ca-cloudline',
          'No fragment element sets came back, so nothing is drawn.');
      } else {
        var text = 'Drawing ' + drawn + (drawn === sets.length ? '' : ' of ' + sets.length) +
          ' fragments (limit ' + MAX_FRAGMENTS + ').';
        if (isNum(data.tles_skipped) && data.tles_skipped > 0) {
          text += ' ' + data.tles_skipped + ' could not be fitted to an element set.';
        }
        cloudLine = add('breakup', 'ca-cloudline', text);
        if (typeof ctx.addIntelItem === 'function') {
          try {
            ctx.addIntelItem('Breakup preview (hypothetical): ' + drawn + ' fragments drawn');
          } catch (e) { /* the feed is optional */ }
        }
      }
    }

    var RENDER = { compare: renderCompare, maneuver: renderManeuver, breakup: renderBreakup };

    // ----- requests -----

    // Resolves to { ok, status, body, timedOut } whatever happens, so no caller needs its
    // own catch. The timer and the abort belong to the run, so a reset can cancel both.
    function post(path, payload, runState) {
      return new Promise(function (resolve) {
        var done = false;
        function finish(result) {
          if (done) return;
          done = true;
          if (runState.timer != null) { clearTimeout(runState.timer); runState.timer = null; }
          resolve(result);
        }
        var controller = typeof AbortController === 'function' ? new AbortController() : null;
        runState.controller = controller;
        runState.timer = setTimeout(function () {
          runState.timer = null;
          finish({ ok: false, status: 0, timedOut: true });
          if (controller) { try { controller.abort(); } catch (e) { /* already settled */ } }
        }, timeoutMs);
        var request;
        try {
          var options = { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload) };
          if (controller) options.signal = controller.signal;
          request = fetch(api + path, options);
        } catch (e) {
          finish({ ok: false, status: 0, body: null });
          return;
        }
        request.then(function (resp) {
          return resp.json().then(
            function (data) { finish({ ok: resp.ok, status: resp.status, body: data }); },
            function () { finish({ ok: resp.ok, status: resp.status, body: null, unreadable: true }); });
        }, function () { finish({ ok: false, status: 0, body: null }); });
      });
    }

    function cancel(runState) {
      if (!runState) return;
      if (runState.timer != null) { clearTimeout(runState.timer); runState.timer = null; }
      if (runState.controller) { try { runState.controller.abort(); } catch (e) { /* settled */ } }
    }

    function setBusy(action, busy) { parts[action].button.disabled = busy; }

    function run(action) {
      if (runs[action]) return;
      var ev = currentEvent();
      if (!ev) {
        say(action, 'Open a conjunction on the globe first.', true);
        return;
      }
      clearResult(action);
      var built = { compare: buildCompareBody, maneuver: buildManeuverBody,
        breakup: buildBreakupBody }[action](ev);
      if (!built.ok) {
        say(action, LEAD[action] + ' failed. ' + built.error, true);
        return;
      }
      if (action === 'breakup') clearCloud();
      var state = { key: eventKey(ev), event: ev, built: built, timer: null, controller: null };
      runs[action] = state;
      setBusy(action, true);
      say(action, RUNNING[action], false);

      post(PATH[action], built.body, state).then(function (res) {
        if (runs[action] !== state) return;
        runs[action] = null;
        setBusy(action, false);
        if (eventKey(currentEvent()) !== state.key) {
          clearResult(action);
          return;
        }
        if (!res.ok || res.unreadable || !validAnswer(action, res.body)) {
          var why;
          if (res.timedOut) why = describeTimeout(timeoutMs);
          else if (!res.ok) why = describeError(res.status, res.body);
          else why = NOT_EXPECTED;
          say(action, LEAD[action] + ' failed. ' + why, true);
          return;
        }
        say(action, '', false);
        try {
          RENDER[action](state, res.body);
        } catch (e) {
          clearResult(action);
          clearCloud();
          say(action, LEAD[action] + ' failed. ' + NOT_EXPECTED, true);
        }
      });
    }

    // A new event, or none: forget everything shown and cancel anything in flight.
    function reset() {
      Object.keys(runs).forEach(function (action) {
        cancel(runs[action]);
        runs[action] = null;
        setBusy(action, false);
        clearResult(action);
      });
      clearCloud();
    }

    function onOpen(e) { opened = e && e.detail ? e.detail : null; reset(); }
    function onClose() { opened = null; reset(); }

    root.appendChild(makeBlock('compare', 'Compare Pc methods'));
    root.appendChild(makeBlock('maneuver', 'Avoidance options'));
    root.appendChild(makeBlock('breakup', 'Breakup preview'));
    ctx.mount.appendChild(root);
    doc.addEventListener('sda:conjunction-open', onOpen);
    doc.addEventListener('sda:conjunction-close', onClose);

    function destroy() {
      if (gone) return;
      gone = true;
      doc.removeEventListener('sda:conjunction-open', onOpen);
      doc.removeEventListener('sda:conjunction-close', onClose);
      Object.keys(runs).forEach(function (action) { cancel(runs[action]); runs[action] = null; });
      clearCloud();
      if (root.parentNode) root.parentNode.removeChild(root);
      if (active === inst) active = null;
    }

    return inst;
  }

  global.SDAConjActions = {
    init: init,
    buildCompareBody: buildCompareBody,
    buildManeuverBody: buildManeuverBody,
    buildBreakupBody: buildBreakupBody,
    sortOptions: sortOptions,
    describeError: describeError,
    describeTimeout: describeTimeout,
    formatPc: formatPc,
    pcBand: pcBand,
    parseTleSets: parseTleSets,
    MAX_FRAGMENTS: MAX_FRAGMENTS,
    CLOUD_INTERVAL_MS: CLOUD_INTERVAL_MS,
    REQUEST_TIMEOUT_MS: REQUEST_TIMEOUT_MS
  };
})(window);
