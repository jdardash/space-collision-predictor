// Agent chat panel for the WorldView globe: a transcript and an input that talk
// to the server's agent (POST /agent/chat), show the tool calls it made, and
// carry out the view actions it returns (fly_to).
//
// Everything that does not need a DOM is a plain function on window.SDAAgentPanel
// so it can be tested under Node: request body, transcript entries, client
// actions, error sentences, health text. The DOM layer below only wires those to
// elements. Model output and tool results are untrusted text, so every string
// reaches the page through textContent and nothing here uses an HTML parser.
//
// Request shape (sda/routes/agent.py, AgentChatRequest): the server keeps the
// conversation under session_id and appends "messages" to it, so after the first
// answer the panel sends only the new user turn plus the session id. Resending
// the whole transcript would put every earlier turn into the model's context
// twice.
//
// Units: the scene sent to the server uses geodetic degrees and kilometres, the
// same names as the agent's scene schema (camera.latitude_deg, longitude_deg,
// altitude_km). Camera heights handed to Cesium are metres above the ellipsoid.

(function (global) {
  'use strict';

  var DEG = Math.PI / 180.0;

  // One agent turn is up to six provider round trips (MAX_ITERATIONS in
  // sda/agent.py), each capped by SDA_LLM_TIMEOUT_S, 60 s by default. The client
  // waits for all of that plus a minute, so the panel never gives up on a turn
  // the server is still legitimately working on.
  var REQUEST_TIMEOUT_MS = 420000;
  var HEALTH_TIMEOUT_MS = 8000;
  var MAX_ITERATIONS_LIMIT = 12;      // le=12 on AgentChatRequest.max_iterations

  var DEFAULT_FLY_HEIGHT_KM = 3000;   // camera standoff when fly_to names no altitude
  var MIN_FLY_HEIGHT_KM = 0.1;
  var MAX_FLY_HEIGHT_KM = 500000;     // the fly_to schema's own ceiling
  var FLY_DURATION_S = 2.5;

  var ARG_VALUE_CHARS = 32;
  var ARG_LINE_CHARS = 90;
  var TOOL_ERROR_CHARS = 140;
  var HINT_CHARS = 300;

  var SUPPORTED_ACTIONS = { fly_to: true };

  // One-click prompts. Each names the tools it leans on, and the tests check
  // those names against the server's real tool catalogue (GET /agent/tools), so
  // a starter can never promise something the agent does not have.
  var STARTERS = [
    {
      label: 'What am I looking at',
      prompt: 'Summarize what I am looking at on the globe right now.',
      tools: ['summarize_scene']
    },
    {
      label: 'Close approaches today',
      prompt: 'Run a conjunction screen for the next 24 hours with a 5 km threshold ' +
        'and list the closest approaches.',
      tools: ['run_conjunction_screen']
    },
    {
      label: 'Show me the ISS',
      prompt: 'Show me the ISS and give me its current altitude and speed.',
      tools: ['list_satellites', 'get_satellite_state', 'fly_to']
    },
    {
      label: 'ISS passes over Austin',
      prompt: 'When does the ISS next pass over Austin, Texas (30.27 N, 97.74 W) ' +
        'in the next 24 hours?',
      tools: ['predict_passes']
    },
    {
      label: 'Decay of selected sat',
      prompt: 'How long will the satellite I have selected stay in orbit?',
      tools: ['summarize_scene', 'estimate_decay']
    }
  ];

  // ---------------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------------

  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }

  function isObject(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function round(v, places) {
    var f = Math.pow(10, places);
    return Math.round(v * f) / f;
  }

  function clip(text, limit) {
    var s = String(text);
    return s.length > limit ? s.slice(0, limit - 3) + '...' : s;
  }

  // A satellite record from satEngine.byNoradId, or null. The map is a plain
  // object keyed by NORAD id and is replaced wholesale when the catalogue
  // reloads, so callers pass the live map each time rather than keeping one.
  function lookupSat(byNoradId, noradId) {
    if (!byNoradId || noradId == null) return null;
    if (typeof byNoradId.get === 'function') return byNoradId.get(noradId) || null;
    return Object.prototype.hasOwnProperty.call(byNoradId, noradId) ? byNoradId[noradId] : null;
  }

  // "7 s" or "1 min 12 s". Anything that is not a non-negative number is 0.
  function formatElapsed(seconds) {
    var n = Number(seconds);
    if (!isFinite(n) || n < 0) n = 0;
    var s = Math.floor(n);
    if (s < 60) return s + ' s';
    return Math.floor(s / 60) + ' min ' + (s % 60) + ' s';
  }

  // ---------------------------------------------------------------------
  // Request
  // ---------------------------------------------------------------------

  // The JSON body for POST /agent/chat. `conversation.sessionId` is null until
  // the first answer comes back; after that only the new turn is sent.
  function buildRequestBody(conversation, text, scene, maxIterations) {
    var body = {};
    var sid = conversation && conversation.sessionId;
    if (typeof sid === 'string' && sid) body.session_id = sid;
    body.messages = [{ role: 'user', content: String(text == null ? '' : text).trim() }];
    if (isObject(scene)) body.scene = scene;
    if (isNum(maxIterations) && maxIterations === Math.floor(maxIterations) &&
        maxIterations >= 1 && maxIterations <= MAX_ITERATIONS_LIMIT) {
      body.max_iterations = maxIterations;
    }
    return body;
  }

  // The scene description the agent's summarize_scene tool reads. Inputs are the
  // raw pieces so this stays testable: camera in radians and metres (a Cesium
  // Cartographic), the selected id and live satellite map, layer names, and an
  // ISO time. Whatever is unknown is left out rather than guessed.
  function buildScene(parts) {
    var p = parts || {};
    var scene = {};
    var cam = p.camera;
    if (cam && isNum(cam.latitude) && isNum(cam.longitude) && isNum(cam.height)) {
      scene.camera = {
        latitude_deg: round(cam.latitude / DEG, 4),
        longitude_deg: round(cam.longitude / DEG, 4),
        altitude_km: round(cam.height / 1000.0, 1)
      };
    }
    if (Array.isArray(p.visibleLayers)) {
      scene.visible_layers = p.visibleLayers.map(function (name) { return String(name); });
    }
    var selected = [];
    if (p.selectedId != null) {
      var sat = lookupSat(p.byNoradId, p.selectedId);
      var id = Number(p.selectedId);
      selected.push({
        norad_id: isFinite(id) ? id : null,
        name: sat && sat.name ? String(sat.name) : ''
      });
    }
    scene.selected = selected;
    if (typeof p.timeIso === 'string' && p.timeIso) scene.time_utc = p.timeIso;
    return scene;
  }

  // ---------------------------------------------------------------------
  // Response to transcript
  // ---------------------------------------------------------------------

  // One short line for a tool call's arguments: key=value pairs, long values cut.
  function summarizeArgs(args) {
    if (!isObject(args)) return '';
    var parts = [];
    for (var key in args) {
      if (!Object.prototype.hasOwnProperty.call(args, key)) continue;
      var v = args[key];
      var shown;
      if (v === null || v === undefined) shown = 'null';
      else if (typeof v === 'object') shown = JSON.stringify(v);
      else shown = String(v);
      shown = shown.replace(/\s+/g, ' ');
      parts.push(key + '=' + clip(shown, ARG_VALUE_CHARS));
    }
    return clip(parts.join(', '), ARG_LINE_CHARS);
  }

  function readCoords(action) {
    var lat = action.latitude_deg;
    var lon = action.longitude_deg;
    if (!isNum(lat) || !isNum(lon)) return null;
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return { lat: lat, lon: lon };
  }

  function readNorad(action) {
    if (action.norad_id == null) return null;
    var n = Number(action.norad_id);
    return (isFinite(n) && n === Math.floor(n) && n > 0) ? n : null;
  }

  function trimNumber(n) {
    return String(round(n, 4));
  }

  // The words for one client action, with no knowledge of what the globe holds.
  function describeAction(action) {
    var a = isObject(action) ? action : {};
    var label = (typeof a.label === 'string' && a.label) ? a.label : '';
    var norad = readNorad(a);
    var coords = readCoords(a);
    var subject;
    if (norad !== null) {
      var name = (typeof a.name === 'string' && a.name) ? a.name : '';
      subject = name ? name + ' (NORAD ' + norad + ')' : 'NORAD ' + norad;
      return 'Fly to ' + subject + (label ? ', "' + label + '"' : '');
    }
    if (coords) {
      var where = trimNumber(coords.lat) + ', ' + trimNumber(coords.lon);
      return 'Fly to ' + (label ? label + ' (' + where + ')' : where);
    }
    return 'Fly to ' + (label ? label : '(no target)');
  }

  // The client actions a response carries, in order. Types this panel does not
  // know are dropped, so a newer server can add one without breaking an older page.
  function extractActions(resp) {
    var out = [];
    if (!resp || !Array.isArray(resp.actions)) return out;
    for (var i = 0; i < resp.actions.length; i++) {
      var item = resp.actions[i];
      if (isObject(item) && typeof item.type === 'string' && SUPPORTED_ACTIONS[item.type] === true) {
        out.push(item);
      }
    }
    return out;
  }

  function disclaimerOf(resp) {
    return (resp && typeof resp.disclaimer === 'string') ? resp.disclaimer : '';
  }

  // AgentChatResponse -> transcript entries, in display order: the answer, the
  // tool calls, the view actions, a note when the loop was cut short, and which
  // provider and model answered (with the client-measured wait when known).
  function responseToEntries(resp, elapsedSeconds) {
    var r = isObject(resp) ? resp : {};
    var entries = [];

    var text = typeof r.text === 'string' ? r.text.trim() : '';
    entries.push(text ? { kind: 'answer', text: text }
                      : { kind: 'note', text: 'The model returned no text.' });

    var trace = Array.isArray(r.trace) ? r.trace : [];
    for (var i = 0; i < trace.length; i++) {
      var call = trace[i];
      if (!isObject(call) || typeof call.name !== 'string' || !call.name) continue;
      var ok = call.ok === true;
      var line = call.name + '(' + summarizeArgs(call.arguments) + ')';
      if (!ok) line += ' failed: ' + clip(call.error || 'unknown error', TOOL_ERROR_CHARS);
      entries.push({ kind: 'tool', name: call.name, ok: ok, text: line });
    }

    var actions = extractActions(r);
    for (var j = 0; j < actions.length; j++) {
      entries.push({ kind: 'action', text: describeAction(actions[j]), action: actions[j] });
    }

    if (r.stop_reason === 'max_iterations') {
      entries.push({
        kind: 'note',
        text: 'Stopped at the tool-call limit before the model finished; ' +
          'the tool calls above hold what was computed.'
      });
    }

    var provider = typeof r.provider === 'string' ? r.provider : '';
    var model = typeof r.model === 'string' ? r.model : '';
    var who = provider && model ? provider + ' / ' + model : (provider || model);
    if (who) {
      if (isNum(elapsedSeconds)) who += ', ' + formatElapsed(elapsedSeconds);
      entries.push({ kind: 'meta', text: who });
    }
    return entries;
  }

  // ---------------------------------------------------------------------
  // Client actions
  // ---------------------------------------------------------------------

  function satPosition(sat) {
    if (!sat || !isNum(sat.lastLat) || !isNum(sat.lastLon)) return null;
    return { lat: sat.lastLat, lon: sat.lastLon, altKm: isNum(sat.lastAlt) ? sat.lastAlt : 0 };
  }

  function clampHeightKm(km) {
    return Math.max(MIN_FLY_HEIGHT_KM, Math.min(MAX_FLY_HEIGHT_KM, km));
  }

  // Decide what a fly_to action means for this globe. The server's action
  // (sda/agent.py, _tool_fly_to) can carry norad_id (with a name when the server
  // knows it), latitude_deg and longitude_deg, altitude_km (camera standoff) and
  // a label, in any combination that has a norad_id or both coordinates.
  //
  //   satellite on the globe  select it; the page's own selection already frames
  //                           it, so a camera flight is added only when the
  //                           action gave an explicit standoff
  //   satellite not on globe  fall back to the coordinates, if it sent any
  //   coordinates only        fly the camera there
  //
  // The returned plan is data; applying it is the DOM layer's job.
  function planFlyTo(action, byNoradId) {
    var a = isObject(action) ? action : {};
    var plan = {
      ok: false, noradId: null, select: false, flight: null,
      label: (typeof a.label === 'string') ? a.label : '',
      reason: '', text: describeAction(a)
    };

    var norad = readNorad(a);
    var coords = readCoords(a);
    var standoff = isNum(a.altitude_km) ? a.altitude_km : null;
    var sat = norad !== null ? lookupSat(byNoradId, norad) : null;

    if (sat) {
      plan.ok = true;
      plan.noradId = norad;
      plan.select = true;
      if (standoff !== null) {
        var pos = satPosition(sat);
        if (pos) {
          plan.flight = { lat: pos.lat, lon: pos.lon, heightKm: clampHeightKm(pos.altKm + standoff) };
        } else if (coords) {
          plan.flight = { lat: coords.lat, lon: coords.lon, heightKm: clampHeightKm(standoff) };
        }
      }
      return plan;
    }

    if (coords) {
      plan.ok = true;
      plan.flight = {
        lat: coords.lat, lon: coords.lon,
        heightKm: clampHeightKm(standoff !== null ? standoff : DEFAULT_FLY_HEIGHT_KM)
      };
      return plan;
    }

    if (norad !== null) {
      plan.reason = 'NORAD ' + norad + ' is not on the globe, and the action had no coordinates.';
    } else if (a.norad_id != null) {
      plan.reason = 'The satellite id in the action is not a valid NORAD number.';
    } else {
      plan.reason = 'The action named no satellite and no usable coordinates.';
    }
    return plan;
  }

  // ---------------------------------------------------------------------
  // Failures
  // ---------------------------------------------------------------------

  // What a thrown value means. Structured failures from the request code carry a
  // kind already and pass through; a timer-driven abort is a timeout; anything
  // else (fetch rejects with a TypeError when the server is down) is the network.
  function classifyThrown(err, timedOut, timeoutMs) {
    if (err && typeof err === 'object' && typeof err.kind === 'string') return err;
    if (timedOut) return { kind: 'timeout', timeoutMs: timeoutMs || REQUEST_TIMEOUT_MS };
    return { kind: 'network', message: (err && err.message) ? String(err.message) : '' };
  }

  function providerNames(detail) {
    var list = isObject(detail) && Array.isArray(detail.providers) ? detail.providers : [];
    var failed = [];
    var configured = [];
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      if (!isObject(p) || !p.configured || typeof p.name !== 'string') continue;
      configured.push(p.name);
      if (p.last_error) failed.push(p.name);
    }
    return (failed.length ? failed : configured).join(', ');
  }

  // A failure as one plain sentence plus an optional quieter hint line. `err` is
  // {kind: 'http', status, body} for a server answer (body is the parsed JSON,
  // FastAPI's {detail: ...}), or {kind: 'timeout' | 'network' | 'parse'}.
  function describeError(err) {
    var e = isObject(err) ? err : {};
    var out = { message: 'Something went wrong.', hint: '' };

    if (e.kind === 'timeout') {
      var ms = isNum(e.timeoutMs) ? e.timeoutMs : REQUEST_TIMEOUT_MS;
      out.message = 'No answer after ' + formatElapsed(ms / 1000) + '. ' +
        'The model may still be working on it; wait a minute and try again.';
      return out;
    }
    if (e.kind === 'network') {
      out.message = 'Could not reach the server. Check that it is running, then try again.';
      return out;
    }
    if (e.kind === 'parse') {
      out.message = 'The server sent a reply the panel could not read (HTTP ' + e.status + ').';
      return out;
    }
    if (e.kind !== 'http') {
      if (e.message) out.message = 'Something went wrong: ' + e.message;
      return out;
    }

    var status = e.status;
    var body = isObject(e.body) ? e.body : {};
    var detail = body.detail;
    var code = isObject(detail) ? detail.error : '';

    if (status === 503 && code === 'no_llm_provider_configured') {
      out.message = 'The agent has no language model set up on this server.';
      out.hint = typeof detail.setup === 'string' ? detail.setup : '';
    } else if (status === 502 && code === 'llm_providers_failed') {
      var names = providerNames(detail);
      out.message = 'The language model' + (names ? ' (' + names + ')' : '') +
        ' could not answer that. Try again in a moment.';
      out.hint = typeof detail.message === 'string' ? clip(detail.message, HINT_CHARS) : '';
    } else if (status === 422 && typeof detail === 'string') {
      out.message = 'The server rejected the message: ' + detail.replace(/\.$/, '') + '.';
    } else if (status === 422 && Array.isArray(detail) && isObject(detail[0])) {
      var loc = Array.isArray(detail[0].loc) ? detail[0].loc.filter(function (part) {
        return part !== 'body';
      }).join('.') : '';
      out.message = 'The server rejected the request (' + (loc ? loc + ': ' : '') +
        String(detail[0].msg || 'invalid') + ').';
    } else if (status === 429) {
      var wait = isNum(body.retry_after_seconds) ? body.retry_after_seconds : 0;
      out.message = 'Too many requests just now. ' +
        (wait > 0 ? 'Wait ' + wait + ' s and try again.' : 'Wait a few seconds and try again.');
    } else if (status === 404) {
      out.message = 'This server has no agent endpoint. It may be an older build.';
    } else {
      out.message = 'The server answered with an error (HTTP ' + status + ').';
      out.hint = typeof detail === 'string' ? clip(detail, HINT_CHARS) : '';
    }
    return out;
  }

  // GET /agent/health -> {state, text, hint}. States: ready (a provider will
  // answer), cooling (configured but backing off after a failure), unconfigured
  // (the server's own setup text goes in hint), unreachable.
  function describeHealth(health) {
    if (!isObject(health) || !Array.isArray(health.providers)) {
      return { state: 'unreachable', text: 'The agent status could not be read from the server.', hint: '' };
    }
    if (!health.configured) {
      return {
        state: 'unconfigured',
        text: 'No language model is configured on this server.',
        hint: typeof health.setup === 'string' ? health.setup : ''
      };
    }

    var active = health.active_provider;
    var cooling = [];
    var soonest = Infinity;
    var lastError = '';
    for (var i = 0; i < health.providers.length; i++) {
      var p = health.providers[i];
      if (!isObject(p) || typeof p.name !== 'string') continue;
      if (active && p.name === active) {
        return {
          state: 'ready',
          text: 'Answers from ' + p.name + (p.model ? ' (' + p.model + ')' : '') + '.',
          hint: ''
        };
      }
      if (p.configured && !p.available) {
        cooling.push(p.name);
        if (isNum(p.cooldown_remaining_s)) soonest = Math.min(soonest, p.cooldown_remaining_s);
        if (!lastError && typeof p.last_error === 'string') lastError = p.last_error;
      }
    }
    if (active) {
      return { state: 'ready', text: 'Answers from ' + active + '.', hint: '' };
    }
    if (!cooling.length) {
      return { state: 'cooling', text: 'No provider is ready right now.', hint: '' };
    }
    var secs = isFinite(soonest) ? Math.ceil(soonest) : 0;
    var text = cooling.length === 1
      ? cooling[0] + ' is cooling down after a failure (' + secs + ' s left).'
      : cooling.join(', ') + ' are cooling down after failures (' + secs + ' s until the first retries).';
    return { state: 'cooling', text: text, hint: clip(lastError, HINT_CHARS) };
  }

  // ---------------------------------------------------------------------
  // Panel (DOM layer)
  // ---------------------------------------------------------------------

  var STYLE = [
    '.agentpanel-health { font-size: 10px; margin-bottom: 4px; opacity: 0.9; }',
    '.agentpanel-health.agentpanel-bad { color: #ff8800; }',
    '.agentpanel-hint { font-size: 9px; color: var(--hud-dim, #7a5500); white-space: pre-wrap;',
    '  overflow-wrap: anywhere; margin: 2px 0 6px; line-height: 1.35; }',
    '.agentpanel-hint:empty { display: none; }',
    '.agentpanel-starters { display: flex; flex-direction: column; gap: 3px; margin-bottom: 6px; }',
    '.agentpanel .agentpanel-starter, .agentpanel .agentpanel-action {',
    '  text-align: left; padding: 4px 6px; letter-spacing: 0.5px; }',
    '.agentpanel .mode-btn:disabled { opacity: 0.4; cursor: default; }',
    '.agentpanel-log { max-height: 260px; overflow-y: auto; margin-bottom: 6px;',
    '  scrollbar-width: thin; scrollbar-color: rgba(255,176,0,0.3) transparent; }',
    '.agentpanel-log:empty { display: none; }',
    '.agentpanel-entry { margin: 0 0 6px; font-size: 11px; line-height: 1.35;',
    '  white-space: pre-wrap; overflow-wrap: anywhere; }',
    '.agentpanel-user { color: var(--hud-dim, #7a5500); border-left: 2px solid rgba(255,176,0,0.4);',
    '  padding-left: 6px; }',
    '.agentpanel-tool, .agentpanel-meta, .agentpanel-note { font-size: 9px; opacity: 0.6; }',
    '.agentpanel-tool.agentpanel-failed { color: #ff8800; opacity: 0.85; }',
    '.agentpanel .agentpanel-action { display: block; width: 100%; margin: 0 0 3px; font-size: 9px; }',
    '.agentpanel-error { font-size: 10px; color: #ff6050; border: 1px solid rgba(255,80,60,0.3);',
    '  padding: 4px 6px; border-radius: 2px; }',
    '.agentpanel-status { font-size: 10px; color: var(--hud-dim, #7a5500); min-height: 12px; }',
    '.agentpanel-input { width: 100%; box-sizing: border-box; resize: vertical; font-size: 11px;',
    '  font-family: inherit; padding: 5px 6px; border-radius: 2px; color: var(--hud-color, #ffb000);',
    '  background: rgba(255,176,0,0.05); border: 1px solid rgba(255,176,0,0.2); }',
    '.agentpanel-input:focus { outline: none; border-color: var(--hud-color, #ffb000); }',
    '.agentpanel-row { display: flex; gap: 4px; margin-top: 4px; }',
    '.agentpanel-row .mode-btn { flex: 1 1 auto; }',
    '.agentpanel-disclaimer { font-size: 8px; color: var(--hud-dim, #7a5500); line-height: 1.3;',
    '  margin-top: 6px; }',
    '.agentpanel-disclaimer:empty { display: none; }'
  ].join('\n');

  var MAX_LOG_ENTRIES = 300;

  var styleAdded = false;
  var active = null; // the panel built by the latest init(), for focus()

  function addStyles() {
    if (styleAdded || !document.head) return;
    styleAdded = true;
    var style = document.createElement('style');
    style.textContent = STYLE;
    document.head.appendChild(style);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    var b = el('button', 'mode-btn ' + className, label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  // Run a call into the page. Anything the page's callbacks throw stays here:
  // the globe matters more than the chat.
  function guard(fn) {
    try {
      return fn();
    } catch (err) {
      if (global.console && console.warn) console.warn('agent panel: ' + (err && err.message));
      return undefined;
    }
  }

  // fetch with a timer, reduced to a promise of {status, body} (2xx and JSON) or
  // a rejection that is always a structured failure ({kind: http | parse |
  // timeout | network}), never a bare Error.
  function request(url, options, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var timedOut = false;
      var controller = (typeof AbortController === 'function') ? new AbortController() : null;
      var timer = setTimeout(function () {
        timedOut = true;
        if (controller) controller.abort();
        else reject(classifyThrown(null, true, timeoutMs));
      }, timeoutMs);
      function done() { clearTimeout(timer); }
      if (controller) options.signal = controller.signal;

      var pending;
      try {
        pending = fetch(url, options);
      } catch (err) {
        done();
        reject(classifyThrown(err, false, timeoutMs));
        return;
      }
      pending.then(function (res) {
        return res.text().then(function (raw) {
          done();
          var body = null;
          var readable = true;
          try { body = raw ? JSON.parse(raw) : null; } catch (err) { readable = false; }
          if (!res.ok) reject({ kind: 'http', status: res.status, body: body });
          else if (!readable || !isObject(body)) reject({ kind: 'parse', status: res.status });
          else resolve({ status: res.status, body: body });
        });
      }).then(null, function (err) {
        done();
        reject(classifyThrown(err, timedOut, timeoutMs));
      });
    });
  }

  function buildPanel(ctx) {
    addStyles();
    var api = (typeof ctx.api === 'string' ? ctx.api : '').replace(/\/+$/, '');
    var timeoutMs = (isNum(ctx.timeoutMs) && ctx.timeoutMs > 0) ? ctx.timeoutMs : REQUEST_TIMEOUT_MS;

    var root = el('div', 'panel agentpanel');
    var title = el('h3', '', 'Agent');
    var body = el('div', 'panel-body');
    root.appendChild(title);
    root.appendChild(body);
    ctx.mount.appendChild(root);

    // The page wires a collapse toggle onto every .panel h3 it finds at load. If
    // init runs before that pass, this handler is registered first and its
    // stopImmediatePropagation keeps the page's from toggling a second time;
    // if init runs after, the page's pass never sees this h3 at all.
    title.addEventListener('click', function (e) {
      e.stopImmediatePropagation();
      root.classList.toggle('collapsed');
    });

    var health = el('div', 'agentpanel-health');
    body.appendChild(health);

    if (ctx.isStatic) {
      health.textContent = 'The agent needs the live server, so it is off in this static demo.';
      return { focus: function () { return false; } };
    }

    health.textContent = 'Checking the agent...';
    var healthHint = el('div', 'agentpanel-hint');
    var starters = el('div', 'agentpanel-starters');
    var log = el('div', 'agentpanel-log');
    log.setAttribute('role', 'log');
    log.setAttribute('aria-live', 'polite');
    var status = el('div', 'agentpanel-status');
    var input = el('textarea', 'agentpanel-input');
    input.rows = 2;
    input.maxLength = 2000;
    input.placeholder = 'Ask about the satellites on screen';
    input.setAttribute('aria-label', 'Message to the agent');
    var row = el('div', 'agentpanel-row');
    var disclaimer = el('div', 'agentpanel-disclaimer');

    var controls = []; // buttons that sit idle while a request is in flight
    var state = { busy: false, timer: null, startedAt: 0, conversation: { sessionId: null } };

    body.appendChild(healthHint);
    body.appendChild(starters);
    body.appendChild(log);
    body.appendChild(status);
    body.appendChild(input);
    body.appendChild(row);
    body.appendChild(disclaimer);

    function showStatus(text) { status.textContent = text; }

    function showHealth(info) {
      health.className = 'agentpanel-health' + (info.state === 'ready' ? '' : ' agentpanel-bad');
      health.textContent = info.text;
      healthHint.textContent = info.hint;
    }

    function checkHealth() {
      request(api + '/agent/health', { method: 'GET', headers: { Accept: 'application/json' } },
              HEALTH_TIMEOUT_MS)
        .then(function (res) { showHealth(describeHealth(res.body)); },
              function () { showHealth(describeHealth(null)); });
    }

    // ---- Page access -------------------------------------------------------

    function liveSats() {
      return ctx.satEngine ? ctx.satEngine.byNoradId : null;
    }

    function sceneTimeIso() {
      var C = global.Cesium;
      var iso = guard(function () {
        if (C && C.JulianDate && ctx.viewer && ctx.viewer.clock) {
          return C.JulianDate.toDate(ctx.viewer.clock.currentTime).toISOString();
        }
        return null;
      });
      return iso || new Date().toISOString();
    }

    function currentScene() {
      return guard(function () {
        var cart = ctx.viewer && ctx.viewer.camera && ctx.viewer.camera.positionCartographic;
        var layers = (typeof ctx.getVisibleLayers === 'function') ? ctx.getVisibleLayers() : null;
        return buildScene({
          camera: cart ? { latitude: cart.latitude, longitude: cart.longitude, height: cart.height } : null,
          selectedId: ctx.satEngine ? ctx.satEngine.selectedId : null,
          byNoradId: liveSats(),
          visibleLayers: layers,
          timeIso: sceneTimeIso()
        });
      }) || null;
    }

    function feed(text) {
      if (typeof ctx.addIntelItem === 'function') guard(function () { ctx.addIntelItem(text); });
    }

    function flyCamera(flight) {
      var camera = ctx.viewer && ctx.viewer.camera;
      var C = global.Cesium;
      if (!camera || typeof camera.flyTo !== 'function' || !C || !C.Cartesian3) return;
      guard(function () {
        camera.flyTo({
          destination: C.Cartesian3.fromDegrees(flight.lon, flight.lat, flight.heightKm * 1000.0),
          orientation: { heading: 0, pitch: -Math.PI / 2, roll: 0 },
          duration: FLY_DURATION_S
        });
      });
    }

    // Carry out one client action. Returns the plan so the caller can report a
    // view that could not be moved.
    function applyAction(action) {
      var plan = planFlyTo(action, liveSats());
      if (!plan.ok) return plan;
      if (plan.select && typeof ctx.selectSatellite === 'function') {
        guard(function () { ctx.selectSatellite(plan.noradId); });
      }
      if (plan.flight) flyCamera(plan.flight);
      feed('Agent: ' + plan.text);
      return plan;
    }

    function replay(action) {
      var plan = applyAction(action);
      if (!plan.ok) addEntry({ kind: 'note', text: 'Could not move the view: ' + plan.reason });
    }

    // ---- Transcript --------------------------------------------------------

    function addEntry(entry) {
      var node;
      if (entry.kind === 'action') {
        node = button(entry.text, 'agentpanel-action', function () { replay(entry.action); });
      } else if (entry.kind === 'error') {
        node = el('div', 'agentpanel-entry agentpanel-error', entry.text);
        if (entry.hint) node.appendChild(el('div', 'agentpanel-hint', entry.hint));
        if (entry.retryText) {
          node.appendChild(button('Retry', 'agentpanel-retry', function () {
            if (state.busy) return;
            if (node.parentNode) node.parentNode.removeChild(node);
            submit(entry.retryText, true);
          }));
        }
      } else {
        var classes = 'agentpanel-entry agentpanel-' + entry.kind;
        if (entry.kind === 'tool' && entry.ok === false) classes += ' agentpanel-failed';
        node = el('div', classes, entry.text);
      }
      log.appendChild(node);
      while (log.children.length > MAX_LOG_ENTRIES) log.removeChild(log.children[0]);
      log.scrollTop = log.scrollHeight;
      return node;
    }

    // ---- One request at a time --------------------------------------------

    function setBusy(on) {
      for (var i = 0; i < controls.length; i++) controls[i].disabled = on;
    }

    function workingText(seconds) {
      return 'Working... ' + formatElapsed(seconds) +
        (seconds >= 15 ? ' (a local model can take a minute or more)' : '');
    }

    function finish() {
      state.busy = false;
      if (state.timer !== null) clearInterval(state.timer);
      state.timer = null;
      showStatus('');
      setBusy(false);
    }

    function onAnswer(resp, elapsed) {
      try {
        if (typeof resp.session_id === 'string' && resp.session_id) {
          state.conversation.sessionId = resp.session_id;
        }
        var notes = [];
        extractActions(resp).forEach(function (action) {
          var plan = applyAction(action);
          if (!plan.ok) notes.push({ kind: 'note', text: 'Could not move the view: ' + plan.reason });
        });
        var entries = responseToEntries(resp, elapsed);
        var last = entries[entries.length - 1];
        var at = (last && last.kind === 'meta') ? entries.length - 1 : entries.length;
        entries.splice.apply(entries, [at, 0].concat(notes));
        entries.forEach(addEntry);
        var legal = disclaimerOf(resp);
        if (legal) disclaimer.textContent = legal;
      } finally {
        finish();
      }
    }

    function onFailure(err, message) {
      try {
        var info = describeError(err);
        addEntry({ kind: 'error', text: info.message, hint: info.hint, retryText: message });
      } finally {
        finish();
        checkHealth(); // the status line may have changed (a provider now cooling down)
      }
    }

    // Send `text` (default: the input box). A retry resends without adding a
    // second copy of the user's message to the transcript.
    function submit(text, isRetry) {
      if (state.busy) return;
      var message = String(text == null ? input.value : text).trim();
      if (!message) return;

      state.busy = true;
      setBusy(true);
      if (!isRetry) {
        addEntry({ kind: 'user', text: message });
        if (text == null) input.value = '';
      }
      state.startedAt = Date.now();
      showStatus(workingText(0));
      state.timer = setInterval(function () {
        showStatus(workingText((Date.now() - state.startedAt) / 1000));
      }, 1000);

      var payload = buildRequestBody(state.conversation, message, currentScene(), ctx.maxIterations);
      request(api + '/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(payload)
      }, timeoutMs).then(function (res) {
        onAnswer(res.body, (Date.now() - state.startedAt) / 1000);
      }, function (err) {
        onFailure(err, message);
      }).then(null, function (err) {
        // Rendering itself failed. The globe and the panel stay usable.
        if (global.console && console.warn) console.warn('agent panel: ' + (err && err.message));
        finish();
      });
    }

    // ---- Controls ----------------------------------------------------------

    for (var i = 0; i < STARTERS.length; i++) {
      (function (starter) {
        var b = button(starter.label, 'agentpanel-starter', function () { submit(starter.prompt); });
        b.title = starter.prompt;
        starters.appendChild(b);
        controls.push(b);
      })(STARTERS[i]);
    }

    // Keys typed here must not reach the page's global handler, where "i" is the
    // basemap toggle and space is play/pause.
    function stop(e) { e.stopPropagation(); }
    input.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        submit();
      } else if (e.key === 'Escape') {
        input.blur();
      }
    });
    input.addEventListener('keypress', stop);
    input.addEventListener('keyup', stop);
    // Space on a focused button would otherwise also pause the simulation clock.
    ['keydown', 'keypress', 'keyup'].forEach(function (type) {
      root.addEventListener(type, function (e) {
        if (e.key === ' ' || e.key === 'Spacebar') e.stopPropagation();
      });
    });

    var sendBtn = button('Send', 'agentpanel-send', function () { submit(); });
    row.appendChild(sendBtn);
    controls.push(sendBtn);

    // Dictation uses the browser's own SpeechRecognition where it exists. In
    // Chrome that sends audio to the browser vendor's speech service; nothing
    // else is called and no key is needed. Hidden where the API is missing.
    var Recognition = global.SpeechRecognition || global.webkitSpeechRecognition;
    if (typeof Recognition === 'function') {
      var listening = null;
      var micBtn = button('Mic', 'agentpanel-mic', function () {
        if (state.busy) return;
        if (listening) {
          guard(function () { listening.stop(); });
          return;
        }
        try {
          var rec = new Recognition();
          rec.lang = (global.navigator && global.navigator.language) || 'en-US';
          rec.interimResults = false;
          rec.continuous = false;
          rec.maxAlternatives = 1;
          rec.onresult = function (ev) {
            var said = '';
            var results = (ev && ev.results) || [];
            for (var r = (ev && ev.resultIndex) || 0; r < results.length; r++) {
              var res = results[r];
              if (res && res.isFinal === false) continue;
              if (res && res[0] && typeof res[0].transcript === 'string') said += res[0].transcript;
            }
            said = said.trim();
            if (said) input.value = input.value ? input.value.replace(/\s+$/, '') + ' ' + said : said;
          };
          rec.onerror = function (ev) {
            var code = ev && ev.error;
            showStatus(code === 'not-allowed' || code === 'service-not-allowed'
              ? 'Microphone access was refused by the browser.'
              : code === 'no-speech' ? 'No speech was heard.'
              : 'Microphone dictation failed' + (code ? ' (' + code + ')' : '') + '.');
          };
          rec.onend = function () {
            listening = null;
            micBtn.classList.remove('active');
            if (status.textContent === 'Listening...') showStatus('');
          };
          listening = rec;
          micBtn.classList.add('active');
          showStatus('Listening...');
          rec.start();
        } catch (err) {
          listening = null;
          micBtn.classList.remove('active');
          showStatus('Microphone dictation could not start.');
        }
      });
      micBtn.title = "Dictate with the browser's built-in speech recognition";
      row.appendChild(micBtn);
      controls.push(micBtn);
    }

    var clearBtn = button('New chat', 'agentpanel-clear', function () {
      if (state.busy) return;
      state.conversation = { sessionId: null };
      while (log.firstChild) log.removeChild(log.firstChild);
      showStatus('');
    });
    row.appendChild(clearBtn);
    controls.push(clearBtn);

    checkHealth();

    return {
      focus: function () {
        root.classList.remove('collapsed');
        guard(function () { if (root.scrollIntoView) root.scrollIntoView({ block: 'nearest' }); });
        input.focus();
        return true;
      }
    };
  }

  // Build the panel inside ctx.mount. Called once, after the globe and the
  // satellite layer are up. Never throws: a broken chat panel must not take the
  // globe with it.
  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof document === 'undefined') return;
      active = buildPanel(ctx);
    } catch (err) {
      if (global.console && console.warn) console.warn('agent panel failed to start: ' + (err && err.message));
    }
  }

  // Put the cursor in the input (opening the panel if it is collapsed). The page
  // decides which key calls this; the panel registers no shortcut of its own.
  function focus() {
    if (!active) return false;
    return guard(function () { return active.focus(); }) === true;
  }

  global.SDAAgentPanel = {
    init: init,
    focus: focus,
    REQUEST_TIMEOUT_MS: REQUEST_TIMEOUT_MS,
    DEFAULT_FLY_HEIGHT_KM: DEFAULT_FLY_HEIGHT_KM,
    STARTERS: STARTERS,
    lookupSat: lookupSat,
    formatElapsed: formatElapsed,
    buildRequestBody: buildRequestBody,
    buildScene: buildScene,
    summarizeArgs: summarizeArgs,
    describeAction: describeAction,
    extractActions: extractActions,
    disclaimerOf: disclaimerOf,
    responseToEntries: responseToEntries,
    planFlyTo: planFlyTo,
    classifyThrown: classifyThrown,
    describeError: describeError,
    describeHealth: describeHealth
  };
})(typeof window !== 'undefined' ? window : this);
