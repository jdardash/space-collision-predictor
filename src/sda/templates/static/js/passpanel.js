// Pass panel for the WorldView globe: when does the selected satellite rise,
// peak and set over an observer you pick.
//
// Everything shown comes from the server. POST /passes and POST /lookangles
// (sda.routes.passes) do the propagation in one frame; this file only builds
// the request bodies, turns the answers into display rows and draws them. There
// is deliberately no orbital math here, so the browser can never disagree with
// the server about where a satellite is.
//
// The pure helpers at the top need no DOM and are exported for the Node tests
// in tests/test_globe_passpanel.py. init() is the only part that touches the
// page.

(function (global) {
  'use strict';

  // Limits mirror sda.passes.Observer so a request the panel sends never 422s
  // for a range reason. Altitude is typed in metres (what a person and the
  // browser's geolocation both think in); the route wants kilometres.
  var LAT_RANGE = [-90, 90];
  var LON_RANGE = [-360, 360];
  var ALT_M_RANGE = [-500, 100000];

  var MAX_MESSAGE_CHARS = 150;

  // ---------------------------------------------------------------------
  // Parsing and validation
  // ---------------------------------------------------------------------

  // A plain decimal, or NaN. Number('') is 0 and Number('1e3') is 1000, and
  // neither is what a person typing into a latitude box means, so the text is
  // checked against a strict pattern first.
  function parseNumber(value) {
    if (typeof value === 'number') return isFinite(value) ? value : NaN;
    if (typeof value !== 'string') return NaN;
    var text = value.replace(/^\s+|\s+$/g, '');
    if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(text)) return NaN;
    return parseFloat(text);
  }

  function isBlank(value) {
    return value == null || (typeof value === 'string' && /^\s*$/.test(value));
  }

  // A NORAD id as a positive integer, or null. Accepts the digits-only string
  // the page keeps in some dataset attributes.
  function toNoradId(value) {
    var n = value;
    if (typeof n === 'string' && /^\s*\d+\s*$/.test(n)) n = parseInt(n, 10);
    if (typeof n !== 'number' || !isFinite(n) || n <= 0 || Math.floor(n) !== n) return null;
    return n;
  }

  function selectedNoradId(engine) {
    return toNoradId(engine ? engine.selectedId : null);
  }

  // Check the three input values and return either
  //   { ok: true, observer: { latitude_deg, longitude_deg, altitude_km } }
  // in the shape sda.passes.Observer takes, or { ok: false, error: '...' }.
  // `raw` is { lat, lon, alt } as typed (strings) or as filled in (numbers).
  // A blank altitude means ground level.
  function validateObserver(raw) {
    var r = raw || {};
    var lat = parseNumber(r.lat);
    if (isNaN(lat)) return { ok: false, error: 'Enter the latitude as a number of degrees.' };
    if (lat < LAT_RANGE[0] || lat > LAT_RANGE[1]) {
      return { ok: false, error: 'Latitude must be between -90 and 90 degrees.' };
    }
    var lon = parseNumber(r.lon);
    if (isNaN(lon)) return { ok: false, error: 'Enter the longitude as a number of degrees.' };
    if (lon < LON_RANGE[0] || lon > LON_RANGE[1]) {
      return { ok: false, error: 'Longitude must be between -360 and 360 degrees.' };
    }
    var altM = 0;
    if (!isBlank(r.alt)) {
      altM = parseNumber(r.alt);
      if (isNaN(altM)) return { ok: false, error: 'Enter the altitude as a number of metres.' };
      if (altM < ALT_M_RANGE[0] || altM > ALT_M_RANGE[1]) {
        return { ok: false, error: 'Altitude must be between -500 and 100000 metres.' };
      }
    }
    return {
      ok: true,
      observer: { latitude_deg: lat, longitude_deg: lon, altitude_km: altM / 1000 }
    };
  }

  // ---------------------------------------------------------------------
  // Request bodies
  // ---------------------------------------------------------------------

  function copyObserver(observer) {
    if (!observer) return null;
    var lat = observer.latitude_deg;
    var lon = observer.longitude_deg;
    var alt = observer.altitude_km == null ? 0 : observer.altitude_km;
    if (typeof lat !== 'number' || typeof lon !== 'number' || typeof alt !== 'number') return null;
    return { latitude_deg: lat, longitude_deg: lon, altitude_km: alt };
  }

  // Body for POST /passes. The window and elevation floor are left to the
  // route's defaults (24 h above 10 degrees) unless `opts` names them; the
  // response echoes what was used and the panel prints that echo.
  function buildPassRequest(noradId, observer, opts) {
    var id = toNoradId(noradId);
    var obs = copyObserver(observer);
    if (id === null || !obs) return null;
    var body = { observer: obs, norad_id: id };
    var o = opts || {};
    if (o.hours != null) body.hours = o.hours;
    if (o.minElevationDeg != null) body.min_elevation_deg = o.minElevationDeg;
    if (o.start != null) body.start = o.start;
    return body;
  }

  // Body for POST /lookangles: the same observer, one object, now unless
  // `opts.time` pins an instant.
  function buildLookAnglesRequest(noradId, observer, opts) {
    var id = toNoradId(noradId);
    var obs = copyObserver(observer);
    if (id === null || !obs) return null;
    var body = { observer: obs, norad_ids: [id] };
    if (opts && opts.time != null) body.time = opts.time;
    return body;
  }

  // ---------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------

  // toFixed without the "-0.0" artefact.
  function fixed(value, digits) {
    if (typeof value !== 'number' || !isFinite(value)) return '--';
    var text = value.toFixed(digits);
    return /^-0(\.0+)?$/.test(text) ? text.slice(1) : text;
  }

  function text(value) {
    return (typeof value === 'string' && value) ? value : '--';
  }

  // The server writes ISO 8601 with microseconds and a Z or +00:00 suffix.
  // Not every engine parses six fractional digits, and a string with no zone
  // would be read as local time, so both are normalised first.
  function parseTime(iso) {
    if (typeof iso !== 'string') return null;
    var s = iso.replace(/(\.\d{3})\d+/, '$1');
    if (!/(Z|[+-]\d\d:?\d\d)$/i.test(s)) s += 'Z';
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  // Local date and time of one instant in `timeZone` (the browser's zone when
  // omitted). hourCycle h23 keeps midnight at 00 instead of 24.
  function localParts(iso, timeZone) {
    var d = parseTime(iso);
    if (!d) return { date: '--', time: '--' };
    var fmt;
    var base = {
      hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    };
    try {
      fmt = new Intl.DateTimeFormat(
        'en-US', Object.assign({ timeZone: timeZone || undefined }, base));
    } catch (e) {
      fmt = new Intl.DateTimeFormat('en-US', base); // unknown zone name
    }
    var p = {};
    fmt.formatToParts(d).forEach(function (part) { p[part.type] = part.value; });
    return {
      date: p.year + '-' + p.month + '-' + p.day,
      time: p.hour + ':' + p.minute + ':' + p.second
    };
  }

  // "America/New_York (EDT)": the zone the times are shown in, with its
  // abbreviation for the date in question (it changes with daylight saving).
  function zoneLabel(timeZone, when) {
    var zone = timeZone;
    if (!zone) {
      try { zone = new Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { zone = ''; }
    }
    if (!zone) return 'local time';
    var abbr = '';
    try {
      new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'short' })
        .formatToParts(when || new Date())
        .forEach(function (part) { if (part.type === 'timeZoneName') abbr = part.value; });
    } catch (e) { abbr = ''; }
    return (!abbr || abbr === zone) ? zone : zone + ' (' + abbr + ')';
  }

  // 405 -> "6m 45s", 59 -> "59s", 3725 -> "1h 02m". Anything that is not a
  // non-negative finite number is "--".
  function formatDuration(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '--';
    var total = Math.round(seconds);
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    function two(n) { return (n < 10 ? '0' : '') + n; }
    if (h > 0) return h + 'h ' + two(m) + 'm';
    if (m > 0) return m + 'm ' + two(s) + 's';
    return s + 's';
  }

  // What the sunlit and visible flags on a pass add up to. `sunlit` is the
  // satellite outside Earth's shadow at its peak; `visible` is sunlit with the
  // sun far enough below the observer's horizon for a dark sky.
  function lightLabel(pass) {
    if (pass.visible === true) return 'Visible, sunlit against a dark sky';
    if (pass.sunlit === true) return 'Sunlit, bright sky';
    if (pass.sunlit === false) return 'In Earth shadow';
    return '';
  }

  // One display row per pass in a /passes response, in the order the server
  // sent them. Every value is a string ready to print; anything the response
  // lacks becomes "--" (or "" for the light label) instead of an exception.
  function passRows(response, opts) {
    var zone = opts ? opts.timeZone : undefined;
    var list = (response && Array.isArray(response.passes)) ? response.passes : [];
    var rows = [];
    list.forEach(function (p) {
      if (!p || typeof p !== 'object') return;
      var rise = localParts(p.rise_time, zone);
      var peak = localParts(p.culmination_time, zone);
      var set = localParts(p.set_time, zone);
      rows.push({
        rise: {
          date: rise.date, time: rise.time,
          compass: text(p.rise_compass), azimuth: fixed(p.rise_azimuth_deg, 0)
        },
        peak: {
          date: peak.date, time: peak.time,
          compass: text(p.culmination_compass), azimuth: fixed(p.culmination_azimuth_deg, 0),
          elevation: fixed(p.max_elevation_deg, 1), range: fixed(p.culmination_range_km, 0)
        },
        set: {
          date: set.date, time: set.time,
          compass: text(p.set_compass), azimuth: fixed(p.set_azimuth_deg, 0)
        },
        duration: formatDuration(p.duration_s),
        light: lightLabel(p),
        partial: p.partial === true
      });
    });
    return rows;
  }

  // The "where is it right now" line from a /lookangles response, or null when
  // the object is missing, errored, or the numbers are not there.
  function lookAnglesLine(response, noradId) {
    var id = toNoradId(noradId);
    var list = (response && Array.isArray(response.look_angles)) ? response.look_angles : [];
    var entry = null;
    for (var i = 0; i < list.length; i++) {
      if (list[i] && toNoradId(list[i].norad_id) === id) { entry = list[i]; break; }
    }
    if (!entry || entry.error) return null;
    var az = entry.azimuth_deg;
    var el = entry.elevation_deg;
    var range = entry.range_km;
    if ([az, el, range].some(function (v) { return typeof v !== 'number' || !isFinite(v); })) {
      return null;
    }
    var up = (typeof entry.above_horizon === 'boolean') ? entry.above_horizon : el > 0;
    var compass = (typeof entry.compass === 'string' && entry.compass) ? ' ' + entry.compass : '';
    return 'az ' + fixed(az, 1) + ' deg' + compass + ', el ' + fixed(el, 1) + ' deg (' +
      (up ? 'above' : 'below') + ' horizon), range ' + fixed(range, 0) + ' km';
  }

  // ---------------------------------------------------------------------
  // Error sentences
  // ---------------------------------------------------------------------

  // Collapse whitespace, cap the length and end on a full stop, so whatever
  // the server said fits on one line of the panel.
  function oneLine(message) {
    var t = String(message).replace(/\s+/g, ' ').replace(/^ | $/g, '');
    if (t.length > MAX_MESSAGE_CHARS) t = t.slice(0, MAX_MESSAGE_CHARS - 3) + '...';
    return /[.!?]$/.test(t) ? t : t + '.';
  }

  // One short sentence for a failed call. `status` is the HTTP status, or 0
  // when no response arrived; `body` is the parsed JSON body or null.
  // FastAPI sends 4xx as { detail: "text" } or, for a 422, a list of
  // { loc: [...], msg: "..." }.
  function describeError(status, body) {
    if (!status) return 'Could not reach the server.';
    if (status === 429) {
      var wait = body ? body.retry_after_seconds : null;
      return (typeof wait === 'number' && isFinite(wait))
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

  function geolocationMessage(code) {
    if (code === 1) return 'Location permission was denied.';
    if (code === 3) return 'The location request timed out.';
    return 'Your location is unavailable.';
  }

  // "2 passes of ISS (ZARYA) in the next 24 h above 10 deg". The window and
  // the elevation floor are read back from the response, which echoes what the
  // route used, so the panel never hard-codes the route's defaults.
  function tidy(n) { return String(Math.round(n * 10) / 10); }

  function summaryLine(response, name, count) {
    var r = response || {};
    var tail = '';
    if (typeof r.hours === 'number') tail += ' in the next ' + tidy(r.hours) + ' h';
    if (typeof r.min_elevation_deg === 'number') {
      tail += ' above ' + tidy(r.min_elevation_deg) + ' deg';
    }
    if (!count) return 'No passes of ' + name + tail + '.';
    return count + (count === 1 ? ' pass' : ' passes') + ' of ' + name + tail;
  }

  // Degrees for a text box: four decimals is about 10 m, and Math.round keeps
  // float noise like 29.999999999999996 out of the field.
  function degText(value) { return String(Math.round(value * 1e4) / 1e4); }

  function radToDeg(rad) { return rad * 180 / Math.PI; }

  function kmToMetres(km) { return Math.round(km * 1e6) / 1e3; }

  // ---------------------------------------------------------------------
  // Panel (DOM)
  // ---------------------------------------------------------------------

  var STYLE_ID = 'passpanel-style';
  var MARKER_COLOR = '#ffb000';

  // Only what the page's own .panel and .mode-btn rules do not already give.
  var CSS = [
    '.passpanel-row { display: flex; align-items: center; gap: 6px; padding: 2px 0;',
    '  font-size: 10px; }',
    '.passpanel-row label { flex: 0 0 36px; font-size: 9px; letter-spacing: 1px;',
    '  color: var(--hud-dim, #7a5500); }',
    '.passpanel-row input { flex: 1; min-width: 0; background: rgba(255,176,0,0.05);',
    '  border: 1px solid rgba(255,176,0,0.15); color: var(--hud-color, #ffb000);',
    '  padding: 3px 5px; font-size: 10px; font-family: inherit; border-radius: 2px;',
    '  outline: none; }',
    '.passpanel-row input:focus { border-color: var(--hud-color, #ffb000); }',
    '.passpanel-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 4px;',
    '  margin: 6px 0 4px; }',
    '.passpanel-go { width: 100%; }',
    '.passpanel-status, .passpanel-note, .passpanel-head, .passpanel-now {',
    '  font-size: 10px; margin-top: 6px; }',
    '.passpanel-error { color: #ff8800; }',
    '.passpanel-zone { font-size: 9px; margin-top: 2px; color: var(--hud-dim, #7a5500); }',
    '.passpanel-list { margin-top: 6px; max-height: 320px; overflow-y: auto;',
    '  scrollbar-width: thin; scrollbar-color: rgba(255,176,0,0.3) transparent; }',
    '.passpanel-pass { padding: 6px 0; border-top: 1px solid rgba(255,176,0,0.06);',
    '  font-size: 10px; }',
    '.passpanel-pass-head { display: flex; justify-content: space-between;',
    '  font-weight: 600; }',
    '.passpanel-line { display: flex; gap: 6px; }',
    '.passpanel-k { flex: 0 0 34px; font-size: 9px; letter-spacing: 1px;',
    '  color: var(--hud-dim, #7a5500); }',
    '.passpanel-light { margin-top: 2px; font-size: 9px; color: var(--hud-dim, #7a5500); }'
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

  // The page's standard panel: <div class="panel"><h3>..</h3><div class="panel-body">..</div></div>
  function buildShell(title) {
    var panel = el('div', 'panel');
    panel.appendChild(el('h3', '', title));
    var body = el('div', 'panel-body');
    panel.appendChild(body);
    return { panel: panel, body: body };
  }

  function labelledInput(label, name, placeholder) {
    var row = el('div', 'passpanel-row');
    var id = 'passpanel-' + name;
    var lab = el('label', '', label);
    lab.htmlFor = id;
    var input = el('input', '');
    input.id = id;
    input.name = name;
    input.type = 'text';
    input.placeholder = placeholder;
    input.setAttribute('inputmode', 'decimal');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('aria-label', name);
    row.appendChild(lab);
    row.appendChild(input);
    return { row: row, input: input };
  }

  function lineRow(key, value) {
    var line = el('div', 'passpanel-line');
    line.appendChild(el('span', 'passpanel-k', key));
    line.appendChild(el('span', '', value));
    return line;
  }

  // One pass as a small block: date and duration, then rise, max and set.
  function passBlock(row) {
    var block = el('div', 'passpanel-pass');
    var head = el('div', 'passpanel-pass-head');
    head.appendChild(el('span', '', row.rise.date));
    head.appendChild(el('span', '', row.duration));
    block.appendChild(head);

    function when(leg) {
      return (leg.date !== row.rise.date ? leg.date + ' ' : '') + leg.time;
    }
    function az(leg) { return when(leg) + '  az ' + leg.azimuth + ' ' + leg.compass; }
    block.appendChild(lineRow('RISE', az(row.rise)));
    block.appendChild(lineRow('MAX', when(row.peak) + '  el ' + row.peak.elevation + ' ' +
      row.peak.compass));
    block.appendChild(lineRow('SET', az(row.set)));
    if (row.light) block.appendChild(el('div', 'passpanel-light', row.light));
    if (row.partial) {
      block.appendChild(el('div', 'passpanel-light', 'Partial: cut off by the search window.'));
    }
    return block;
  }

  function init(ctx) {
    try {
      return mountPanel(ctx || {});
    } catch (e) {
      // The pass panel is optional; a fault in it must never reach the globe.
      if (global.console && global.console.warn) {
        global.console.warn('SDAPassPanel: init failed', e);
      }
      return null;
    }
  }

  function mountPanel(ctx) {
    var mount = ctx.mount;
    if (!mount) return null;
    var api = String(ctx.api || '');
    var shell = buildShell('PASSES');
    injectStyle();

    if (ctx.isStatic) {
      shell.body.appendChild(el('div', 'passpanel-note',
        'Passes need the live server; they are not available in the static demo.'));
      mount.appendChild(shell.panel);
      return { destroy: function () { mount.textContent = ''; } };
    }

    var lat = labelledInput('LAT', 'latitude', 'deg north');
    var lon = labelledInput('LON', 'longitude', 'deg east');
    var alt = labelledInput('ALT', 'altitude', 'metres, blank = 0');
    var viewBtn = el('button', 'mode-btn', 'VIEW CENTER');
    var gpsBtn = el('button', 'mode-btn', 'MY LOCATION');
    var goBtn = el('button', 'mode-btn passpanel-go', 'PASSES FOR SELECTED');
    var buttonRow = el('div', 'passpanel-buttons');
    buttonRow.appendChild(viewBtn);
    buttonRow.appendChild(gpsBtn);
    var status = el('div', 'passpanel-status');
    var now = el('div', 'passpanel-now');
    var list = el('div', 'passpanel-list');

    [lat.row, lon.row, alt.row, buttonRow, goBtn, status, now, list].forEach(function (node) {
      shell.body.appendChild(node);
    });
    mount.appendChild(shell.panel);

    var marker = null;
    var markerKey = '';
    var requestToken = 0;

    function say(message, isError) {
      status.className = isError ? 'passpanel-status passpanel-error' : 'passpanel-status';
      status.textContent = message || '';
    }

    function clearResults() {
      now.textContent = '';
      list.textContent = '';
    }

    function readFields() {
      return { lat: lat.input.value, lon: lon.input.value, alt: alt.input.value };
    }

    // The one globe entity this panel owns. It is replaced when the observer
    // changes and removed when the fields stop describing a valid observer.
    function setMarker(observer) {
      var key = observer
        ? [observer.latitude_deg, observer.longitude_deg, observer.altitude_km].join('|')
        : '';
      if (key === markerKey) return;
      var viewer = ctx.viewer;
      try {
        if (marker && viewer && viewer.entities) viewer.entities.remove(marker);
      } catch (e) { /* the entity is gone with the viewer */ }
      marker = null;
      markerKey = '';
      if (!observer || !viewer || !viewer.entities) return;
      try {
        var C = global.Cesium;
        var amber = C.Color.fromCssColorString(MARKER_COLOR);
        marker = viewer.entities.add({
          name: 'Pass observer',
          position: C.Cartesian3.fromDegrees(
            observer.longitude_deg, observer.latitude_deg, kmToMetres(observer.altitude_km)),
          point: {
            pixelSize: 9,
            color: amber,
            outlineColor: C.Color.fromCssColorString('#000000'),
            outlineWidth: 2,
            disableDepthTestDistance: Number.POSITIVE_INFINITY
          },
          label: {
            text: 'OBSERVER',
            font: '10px monospace',
            fillColor: amber,
            pixelOffset: new C.Cartesian2(0, -16),
            disableDepthTestDistance: Number.POSITIVE_INFINITY
          },
          // The page's click handler reads properties.type for entity info.
          properties: { type: 'observer' }
        });
        markerKey = key;
      } catch (e) {
        marker = null;
      }
    }

    function syncMarkerToFields() {
      var checked = validateObserver(readFields());
      setMarker(checked.ok ? checked.observer : null);
    }

    function setFields(latText, lonText, altText) {
      lat.input.value = latText;
      lon.input.value = lonText;
      alt.input.value = altText;
      syncMarkerToFields();
    }

    // The point under the middle of the screen, or the camera's own
    // sub-point when the middle of the screen is sky.
    function viewCenter() {
      var viewer = ctx.viewer;
      var C = global.Cesium;
      if (!viewer || !viewer.camera || !C) return null;
      try {
        var canvas = viewer.scene.canvas;
        var hit = viewer.camera.pickEllipsoid(
          new C.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2),
          viewer.scene.globe.ellipsoid);
        if (hit) {
          var c = C.Cartographic.fromCartesian(hit);
          return { lat: radToDeg(c.latitude), lon: radToDeg(c.longitude) };
        }
      } catch (e) { /* fall through to the camera position */ }
      var p = viewer.camera.positionCartographic;
      return p ? { lat: radToDeg(p.latitude), lon: radToDeg(p.longitude) } : null;
    }

    function useViewCenter() {
      var point = viewCenter();
      if (!point || !isFinite(point.lat) || !isFinite(point.lon)) {
        say('Could not read the view center.', true);
        return;
      }
      setFields(degText(point.lat), degText(point.lon), '0');
      say('');
    }

    function useMyLocation() {
      var geo = global.navigator ? global.navigator.geolocation : null;
      if (!geo || typeof geo.getCurrentPosition !== 'function') {
        say(geolocationMessage(2), true);
        return;
      }
      say('Waiting for your location...');
      try {
        geo.getCurrentPosition(function (position) {
          var c = position && position.coords;
          if (!c || !isFinite(c.latitude) || !isFinite(c.longitude)) {
            say(geolocationMessage(2), true);
            return;
          }
          var altitude = (typeof c.altitude === 'number' && isFinite(c.altitude))
            ? Math.round(c.altitude) : 0;
          setFields(degText(c.latitude), degText(c.longitude), String(altitude));
          say('');
        }, function (err) {
          say(geolocationMessage(err ? err.code : 0), true);
        }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 });
      } catch (e) {
        say(geolocationMessage(2), true);
      }
    }

    // POST JSON and resolve to { ok, status, body } whatever happens; a
    // thrown fetch, a dropped connection and a non-JSON body all land here
    // as plain values so no caller needs its own catch.
    function post(path, payload) {
      var request;
      try {
        request = fetch(api + path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      } catch (e) {
        return Promise.resolve({ ok: false, status: 0, body: null });
      }
      return request.then(function (resp) {
        return resp.json().then(
          function (data) { return { ok: resp.ok, status: resp.status, body: data }; },
          function () { return { ok: resp.ok, status: resp.status, body: null }; });
      }, function () {
        return { ok: false, status: 0, body: null };
      });
    }

    function render(response, name, noradId, lookResult) {
      var rows = passRows(response);
      var summary = summaryLine(response, name, rows.length);
      var first = parseTime(response && response.passes && response.passes[0]
        ? response.passes[0].rise_time : null);

      now.textContent = '';
      var lookLine = (lookResult && lookResult.ok)
        ? lookAnglesLine(lookResult.body, noradId) : null;
      if (lookLine) {
        now.appendChild(el('span', 'passpanel-k', 'NOW'));
        now.appendChild(el('span', '', lookLine));
      }

      list.textContent = '';
      list.appendChild(el('div', 'passpanel-head', summary));
      if (rows.length) {
        list.appendChild(el('div', 'passpanel-zone',
          'Times in ' + zoneLabel(undefined, first || new Date())));
      }
      rows.forEach(function (row) { list.appendChild(passBlock(row)); });
      return summary;
    }

    // Read the selection and the fields right now, ask the server, draw the
    // answer. A newer click makes an older answer stale, so it is dropped.
    function run() {
      requestToken += 1;
      var token = requestToken;
      clearResults();

      var noradId = selectedNoradId(ctx.satEngine);
      if (noradId === null) {
        say('Select a satellite on the globe first.', true);
        return Promise.resolve();
      }
      var checked = validateObserver(readFields());
      if (!checked.ok) {
        say(checked.error, true);
        setMarker(null);
        return Promise.resolve();
      }
      setMarker(checked.observer);

      var sat = (ctx.satEngine && ctx.satEngine.byNoradId)
        ? ctx.satEngine.byNoradId[noradId] : null;
      var name = (sat && sat.name) ? sat.name : null;

      say('Loading passes...');
      var passCall = post('/passes', buildPassRequest(noradId, checked.observer));
      // Look angles are a bonus line: if they fail the pass list still shows.
      var lookCall = post('/lookangles', buildLookAnglesRequest(noradId, checked.observer));

      return Promise.all([passCall, lookCall]).then(function (results) {
        if (token !== requestToken) return;
        var passResult = results[0];
        if (!passResult.ok) {
          say(describeError(passResult.status, passResult.body), true);
          return;
        }
        var shownName = name;
        var body = passResult.body;
        if (!shownName && body && body.passes && body.passes[0]) {
          shownName = body.passes[0].satellite_name;
        }
        var summary = render(body, shownName || ('NORAD ' + noradId), noradId, results[1]);
        say('');
        if (typeof ctx.addIntelItem === 'function') ctx.addIntelItem('PASSES: ' + summary);
      }).then(null, function () {
        if (token === requestToken) say('The pass panel hit an error.', true);
      });
    }

    function guard(fn) {
      return function () {
        try {
          return fn.apply(null, arguments);
        } catch (e) {
          say('The pass panel hit an error.', true);
          return undefined;
        }
      };
    }

    viewBtn.addEventListener('click', guard(useViewCenter));
    gpsBtn.addEventListener('click', guard(useMyLocation));
    goBtn.addEventListener('click', guard(run));

    // Typing in a field must not trigger the page's global shortcuts (1-9, A,
    // E, S and so on), so every key event stops here. Enter runs the query.
    [lat.input, lon.input, alt.input].forEach(function (input) {
      input.addEventListener('keydown', guard(function (e) {
        e.stopPropagation();
        if (e.key === 'Enter') return run();
        return undefined;
      }));
      input.addEventListener('keyup', function (e) { e.stopPropagation(); });
      input.addEventListener('keypress', function (e) { e.stopPropagation(); });
      input.addEventListener('input', guard(syncMarkerToFields));
      input.addEventListener('change', guard(syncMarkerToFields));
    });

    return {
      destroy: function () {
        requestToken += 1;
        setMarker(null);
        mount.textContent = '';
      }
    };
  }

  global.SDAPassPanel = {
    init: init,
    validateObserver: validateObserver,
    buildPassRequest: buildPassRequest,
    buildLookAnglesRequest: buildLookAnglesRequest,
    selectedNoradId: selectedNoradId,
    passRows: passRows,
    formatDuration: formatDuration,
    lookAnglesLine: lookAnglesLine,
    describeError: describeError,
    geolocationMessage: geolocationMessage,
    zoneLabel: zoneLabel
  };
})(typeof window !== 'undefined' ? window : this);
