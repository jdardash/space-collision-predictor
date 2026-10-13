// Satellite search for the WorldView globe: find an object by name or NORAD
// id, select it and fly the camera to it.
//
// The ranking in rank() is pure (no DOM, no Cesium), so it runs under Node in
// tests/test_globe_satsearch.py. init() builds one sidebar panel inside the
// element the page hands it and keeps no copy of the catalog: the page swaps
// satEngine.sats on every reload, so the list is read again on each query.

(function (global) {
  'use strict';

  var DEFAULT_LIMIT = 12;
  var DEBOUNCE_MS = 90;

  // Match tiers, best first. A digit query tries the id tiers before the
  // name tiers, so typing a full NORAD id always puts that satellite on top.
  var TIER_ID_EXACT = 0;
  var TIER_ID_PREFIX = 1;
  var TIER_NAME_EXACT = 2;
  var TIER_NAME_PREFIX = 3;
  var TIER_NAME_WORD = 4;
  var TIER_NAME_SUBSTRING = 5;

  // ASCII punctuation, spaces and control characters. Names and queries are
  // lower-cased and have each run of these folded to one space, so that
  // "starlink 1007", "Starlink-1007" and "STARLINK_1007" all meet. Letters,
  // digits and anything above ASCII are kept as word characters.
  var SEPARATORS = /[\x00-\x2f\x3a-\x40\x5b-\x60\x7b-\x7f]+/g;

  // ---------------------------------------------------------------------
  // Ranking (pure)
  // ---------------------------------------------------------------------

  function fold(text) {
    return text.toLowerCase().replace(SEPARATORS, ' ').trim();
  }

  // ---------------------------------------------------------------------
  // Alpha-5 NORAD ids
  // ---------------------------------------------------------------------

  // Ported from PremaanshVyas/satlas (https://github.com/PremaanshVyas/satlas),
  // MIT License, Copyright (c) 2026 Premaansh Vyas, commit
  // 410f9238e54859e93ac46e93cc2e626fcfa53bcb, apps/web/src/lib/norad.ts:
  // ALPHA5_DIGITS, noradToInt and noradKey. The full permission notice is in
  // THIRD_PARTY.md.
  //
  // Space-Track writes ids above 99,999 as a letter and four digits: A0000 is
  // 100000, and the letter replaces the first two digits. I and O are skipped
  // so they cannot be misread as 1 and 0.
  //
  // Changed from the donor: it also accepts lower case and one to four digits
  // after the letter (so "A1" would decode to 100001); here the letter must be
  // upper case and exactly four digits follow, because a shorter id is a typo
  // rather than a second spelling of the same object. The search box upper-cases
  // what is typed before decoding, so typing "a0001" still finds it. A finite
  // whole number is also accepted, which the page's records use.
  var ALPHA5_DIGITS = '0123456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  var ALPHA5_ID = /^([A-HJ-NP-Z])(\d{4})$/;

  // Decode a NORAD id (plain or alpha-5) to its integer, or NaN when it is not
  // an id, so "no id" can be told from "id zero".
  function noradToInt(raw) {
    if (typeof raw === 'number') {
      return (isFinite(raw) && raw >= 0 && Math.floor(raw) === raw) ? raw : NaN;
    }
    if (typeof raw !== 'string') return NaN;
    var s = raw.trim();
    if (/^\d+$/.test(s)) return parseInt(s, 10);
    var m = ALPHA5_ID.exec(s);
    if (!m) return NaN;
    return ALPHA5_DIGITS.indexOf(m[1]) * 10000 + parseInt(m[2], 10);
  }

  // One string per object whatever the spelling: '06707', '6707' and 6707 meet,
  // and so do 'A0001' and '100001'. An unparseable id keeps its trimmed text
  // rather than vanishing, so the record can still be addressed.
  function noradKey(raw) {
    var n = noradToInt(raw);
    if (!isNaN(n)) return String(n);
    return (typeof raw === 'string') ? raw.trim() : '';
  }

  // The NORAD id as a digit string, or null when the record has none. The
  // page stores ids as numbers; a numeric string is tolerated, and so is an
  // alpha-5 string, which is decoded.
  function idString(noradId) {
    if (typeof noradId === 'number' && isFinite(noradId) && noradId >= 0) {
      return String(Math.floor(noradId));
    }
    if (typeof noradId === 'string') {
      if (/^\d+$/.test(noradId)) return noradId.replace(/^0+(?=\d)/, '');
      if (ALPHA5_ID.test(noradId)) return String(noradToInt(noradId));
    }
    return null;
  }

  // Tier for a folded name against a folded query, or -1 for no match.
  function nameTier(name, q) {
    if (!name) return -1;
    if (name === q) return TIER_NAME_EXACT;
    if (name.indexOf(q) === 0) return TIER_NAME_PREFIX;
    if (name.indexOf(' ' + q) >= 0) return TIER_NAME_WORD;
    if (name.indexOf(q) >= 0) return TIER_NAME_SUBSTRING;
    return -1;
  }

  function byTierNameId(a, b) {
    if (a.tier !== b.tier) return a.tier - b.tier;
    if (a.name !== b.name) return a.name < b.name ? -1 : 1;
    if (a.id !== b.id) return a.id - b.id;
    return a.index - b.index; // identical name and id: keep catalog order
  }

  // Rank `sats` against `query`. Returns { results, total }: `results` holds
  // at most `limit` of the original records (same objects, not copies) and
  // `total` counts every match, so the caller can say how many were cut.
  function rank(query, sats, limit) {
    var empty = { results: [], total: 0 };
    if (query == null || !sats || typeof sats.length !== 'number') return empty;
    var raw = String(query).trim();
    var q = fold(raw);
    if (!q) return empty;

    var max = (typeof limit === 'number' && limit >= 0) ? Math.floor(limit) : DEFAULT_LIMIT;
    var digits = /^\d+$/.test(raw);
    var idExact = digits ? (raw.replace(/^0+(?=\d)/, '')) : null;
    // A complete alpha-5 id ("A0001") is an exact id query too. It has no
    // prefix tier: a partial one such as "A00" stays a name search.
    if (!digits) {
      var alpha = noradToInt(raw.toUpperCase());
      if (!isNaN(alpha)) idExact = String(alpha);
    }

    var hits = [];
    for (var i = 0; i < sats.length; i++) {
      var s = sats[i];
      if (!s || typeof s !== 'object') continue;
      var id = idString(s.noradId);
      var tier = -1;
      if (idExact !== null && id !== null) {
        if (id === idExact) tier = TIER_ID_EXACT;
        else if (digits && id.indexOf(raw) === 0) tier = TIER_ID_PREFIX;
      }
      var name = (typeof s.name === 'string') ? fold(s.name) : '';
      if (tier < 0) tier = nameTier(name, q);
      if (tier < 0) continue;
      hits.push({
        rec: s, tier: tier, name: name, index: i,
        id: id === null ? Infinity : Number(id)
      });
    }
    hits.sort(byTierNameId);

    var results = [];
    for (var k = 0; k < hits.length && k < max; k++) results.push(hits[k].rec);
    return { results: results, total: hits.length };
  }

  // ---------------------------------------------------------------------
  // Formatting and camera
  // ---------------------------------------------------------------------

  var STYLE_ID = 'satsearch-style';
  var STYLE_CSS = [
    '.satsearch-input { width: 100%; box-sizing: border-box; background: rgba(255,176,0,0.04);',
    '  border: 1px solid rgba(255,176,0,0.15); border-radius: 2px; padding: 6px 8px;',
    '  color: var(--hud-color, #ffb000); font-size: 11px; font-family: inherit;',
    '  letter-spacing: 1px; outline: none; }',
    '.satsearch-input:focus { border-color: var(--hud-color, #ffb000);',
    '  box-shadow: 0 0 8px var(--hud-glow, rgba(255,176,0,0.2)); }',
    '.satsearch-input::placeholder { color: var(--hud-dim, #7a5500); }',
    '.satsearch-list { max-height: 260px; overflow-y: auto; margin-top: 6px;',
    '  scrollbar-width: thin; scrollbar-color: rgba(255,176,0,0.3) transparent; }',
    '.satsearch-row { padding: 5px 6px; cursor: pointer; font-size: 10px;',
    '  border-bottom: 1px solid rgba(255,176,0,0.06); }',
    '.satsearch-row:hover, .satsearch-row.satsearch-active { background: rgba(255,176,0,0.12); }',
    '.satsearch-row.satsearch-active { box-shadow: inset 2px 0 0 var(--hud-color, #ffb000); }',
    '.satsearch-name { display: block; font-size: 11px; font-weight: 600; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.satsearch-meta { display: block; margin-top: 1px; font-size: 9px; letter-spacing: 0.5px;',
    '  color: var(--hud-dim, #7a5500); }',
    '.satsearch-status { margin-top: 6px; font-size: 9px; letter-spacing: 1px;',
    '  color: var(--hud-dim, #7a5500); }',
    '.satsearch-status:empty { display: none; }'
  ].join('\n');

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  // Altitude is "known" only above zero: the page creates every record with
  // lastAlt 0 (and lat/lon 0) until the first propagation lands, and a real
  // orbit is never at sea level.
  function hasPosition(s) {
    return isNum(s.lastLat) && isNum(s.lastLon) && isNum(s.lastAlt) && s.lastAlt > 0;
  }

  function displayName(rec) {
    if (typeof rec.name === 'string' && rec.name.trim()) return rec.name;
    var id = idString(rec.noradId);
    return id === null ? '(unnamed)' : 'NORAD ' + id;
  }

  function metaLine(rec) {
    var parts = [];
    var id = idString(rec.noradId);
    if (id !== null) parts.push('NORAD ' + id);
    if (typeof rec.regime === 'string' && rec.regime) parts.push(rec.regime);
    if (hasPosition(rec)) parts.push(Math.round(rec.lastAlt) + ' km');
    return parts.join(' | ');
  }

  // How far above the satellite the camera stops, in km: far enough that the
  // object and the Earth limb share the frame, growing with orbit height so a
  // GEO satellite is not framed from LEO distance.
  function standoffKm(altKm) {
    return Math.max(1200, Math.min(altKm * 0.6, 25000));
  }

  // Fly to a point straight above the satellite. Cesium's default
  // orientation for a Cartesian destination looks at the planet centre with
  // north up, so the satellite ends up centred below the camera. Returns
  // false when there was nothing to fly to.
  function flyToSat(viewer, rec) {
    var cesium = global.Cesium;
    if (!viewer || !viewer.camera || !cesium || !hasPosition(rec)) return false;
    var camera = viewer.camera;
    // selectSatellite starts its own fly-to in free camera mode; stop it so
    // the two flights do not fight over the same frames.
    if (typeof camera.cancelFlight === 'function') camera.cancelFlight();
    camera.flyTo({
      destination: cesium.Cartesian3.fromDegrees(
        rec.lastLon, rec.lastLat, (rec.lastAlt + standoffKm(rec.lastAlt)) * 1000),
      duration: 1.6
    });
    return true;
  }

  function injectStyle() {
    var doc = global.document;
    if (doc.getElementById(STYLE_ID)) return;
    var style = doc.createElement('style');
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    doc.head.appendChild(style);
  }

  function el(tag, className, text) {
    var node = global.document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // ---------------------------------------------------------------------
  // Panel
  // ---------------------------------------------------------------------

  // One search panel bound to one page context. Every call into the page
  // (select, feed, camera, catalog read) sits in its own try block: a
  // failure there must not take the globe down or stop the other steps.
  function createPanel(ctx) {
    var ui = {
      ctx: ctx,
      results: [],
      rows: [],
      active: -1,
      timer: null
    };

    ui.root = el('div', 'panel');
    var head = el('h3', null, 'Satellite Search');
    var body = el('div', 'panel-body');
    ui.input = el('input', 'satsearch-input');
    ui.list = el('div', 'satsearch-list');
    ui.status = el('div', 'satsearch-status');
    ui.root.appendChild(head);
    ui.root.appendChild(body);
    body.appendChild(ui.input);
    body.appendChild(ui.list);
    body.appendChild(ui.status);

    ui.input.setAttribute('type', 'text');
    ui.input.setAttribute('placeholder', 'Name or NORAD id');
    ui.input.setAttribute('aria-label', 'Search satellites by name or NORAD id');
    ui.input.setAttribute('autocomplete', 'off');
    ui.input.setAttribute('autocapitalize', 'off');
    ui.input.setAttribute('spellcheck', 'false');
    ui.input.setAttribute('maxlength', '64');
    ui.input.setAttribute('role', 'combobox');
    ui.input.setAttribute('aria-autocomplete', 'list');
    ui.input.setAttribute('aria-controls', 'satsearch-list');
    ui.list.id = 'satsearch-list';
    ui.list.setAttribute('role', 'listbox');

    // The page binds a collapse toggle to every .panel h3 once at start-up.
    // Taking the click here first (and stopping it) gives one toggle whether
    // init runs before or after that binding.
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      ui.root.classList.toggle('collapsed');
    });

    // The page's catalog array is replaced on reload, so read it each time.
    ui.catalog = function () {
      try {
        var sats = ui.ctx.satEngine.sats;
        return (sats && typeof sats.length === 'number') ? sats : [];
      } catch (err) {
        return [];
      }
    };

    // The record as the engine holds it now, which carries a fresher position
    // than the one captured when the list was drawn. The index may be keyed by
    // the decoded number while the record carries the alpha-5 text, or the
    // other way round, so a miss on the raw id retries on the normalised key.
    ui.live = function (rec) {
      try {
        var map = ui.ctx.satEngine.byNoradId;
        return (map && (map[rec.noradId] || map[noradKey(rec.noradId)])) || rec;
      } catch (err) {
        return rec;
      }
    };

    ui.clearTimer = function () {
      if (ui.timer !== null) { clearTimeout(ui.timer); ui.timer = null; }
    };

    ui.schedule = function () {
      ui.clearTimer();
      ui.timer = setTimeout(function () { ui.timer = null; ui.run(); }, DEBOUNCE_MS);
    };

    // Run a query that is still waiting on the debounce timer, so a fast
    // typist who hits Enter or an arrow acts on what is in the box.
    ui.flush = function () {
      if (ui.timer !== null) { ui.clearTimer(); ui.run(); }
    };

    ui.render = function (results, message) {
      ui.list.textContent = '';
      ui.rows = [];
      ui.results = results;
      for (var i = 0; i < results.length; i++) {
        var row = ui.buildRow(results[i], i);
        ui.rows.push(row);
        ui.list.appendChild(row);
      }
      ui.status.textContent = message;
      ui.input.setAttribute('aria-expanded', results.length ? 'true' : 'false');
      ui.setActive(results.length ? 0 : -1, false);
    };

    ui.buildRow = function (rec, index) {
      var row = el('div', 'satsearch-row');
      row.id = 'satsearch-opt-' + index;
      row.setAttribute('role', 'option');
      var name = displayName(rec);
      var nameEl = el('span', 'satsearch-name', name);
      nameEl.setAttribute('title', name);
      row.appendChild(nameEl);
      row.appendChild(el('span', 'satsearch-meta', metaLine(rec)));
      // Keep focus in the box when a row is pressed.
      row.addEventListener('mousedown', function (e) {
        try { e.preventDefault(); } catch (err) { /* keep going */ }
      });
      // mousemove, not mouseover: a redraw under a still pointer fires
      // mouseover and would steal the highlight from the arrow keys.
      row.addEventListener('mousemove', function () {
        if (ui.active !== index) ui.setActive(index, false);
      });
      row.addEventListener('click', function () { ui.choose(index); });
      return row;
    };

    ui.setActive = function (index, scroll) {
      ui.active = index;
      for (var i = 0; i < ui.rows.length; i++) {
        var on = (i === index);
        if (on) ui.rows[i].classList.add('satsearch-active');
        else ui.rows[i].classList.remove('satsearch-active');
        ui.rows[i].setAttribute('aria-selected', on ? 'true' : 'false');
      }
      if (index >= 0 && ui.rows[index]) {
        ui.input.setAttribute('aria-activedescendant', ui.rows[index].id);
        if (scroll && typeof ui.rows[index].scrollIntoView === 'function') {
          ui.rows[index].scrollIntoView({ block: 'nearest' });
        }
      } else {
        ui.input.setAttribute('aria-activedescendant', '');
      }
    };

    ui.run = function () {
      var query = String(ui.input.value || '').trim();
      if (!query) { ui.render([], ''); return; }
      var sats = ui.catalog();
      if (!sats.length) { ui.render([], 'Catalog is still loading.'); return; }
      var found = rank(query, sats, DEFAULT_LIMIT);
      var message = '';
      if (!found.total) {
        var shown = query.length > 40 ? query.slice(0, 40) + '...' : query;
        message = 'No satellite matches "' + shown + '".';
      } else if (found.total > found.results.length) {
        message = '+' + (found.total - found.results.length) + ' more, keep typing to narrow.';
      }
      ui.render(found.results, message);
    };

    ui.move = function (delta) {
      var n = ui.results.length;
      if (!n) return;
      ui.setActive((ui.active + delta + n) % n, true);
    };

    // Select, fly, log. The three steps are independent on purpose.
    ui.choose = function (index) {
      var picked = ui.results[index];
      if (!picked) return;
      var rec = ui.live(picked);
      // The id the page's own index uses, which is the live record's.
      var pageId = rec.noradId != null ? rec.noradId : picked.noradId;
      try { ui.ctx.selectSatellite(pageId); } catch (err) { /* keep going */ }
      var flew = false;
      try { flew = flyToSat(ui.ctx.viewer, rec); } catch (err) { /* keep going */ }
      try {
        var id = idString(rec.noradId);
        var line = 'Search: ' + displayName(rec) + (id === null ? '' : ' (NORAD ' + id + ')');
        line += flew ? ', ' + Math.round(rec.lastAlt) + ' km' : ', position not yet available';
        ui.ctx.addIntelItem(line);
      } catch (err) { /* keep going */ }
    };

    ui.escape = function () {
      if (String(ui.input.value || '') !== '') {
        ui.clearTimer();
        ui.input.value = '';
        ui.render([], '');
      } else if (typeof ui.input.blur === 'function') {
        ui.input.blur();
      }
    };

    // Everything typed in the box belongs to the box: stop each key before
    // it reaches the page's document-level shortcuts.
    function onKey(e) {
      try { e.stopPropagation(); } catch (err) { /* keep going */ }
      if (e.type !== 'keydown' || e.isComposing) return;
      try {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          ui.flush();
          ui.move(e.key === 'ArrowDown' ? 1 : -1);
        } else if (e.key === 'Enter') {
          e.preventDefault();
          ui.flush();
          ui.choose(ui.active);
        } else if (e.key === 'Escape') {
          ui.escape();
        }
      } catch (err) {
        /* the globe must keep working */
      }
    }
    ui.input.addEventListener('keydown', onKey);
    ui.input.addEventListener('keyup', onKey);
    ui.input.addEventListener('keypress', onKey);
    ui.input.addEventListener('input', function () { ui.schedule(); });

    ui.focus = function () {
      ui.root.classList.remove('collapsed');
      if (typeof ui.root.scrollIntoView === 'function') {
        ui.root.scrollIntoView({ block: 'nearest' });
      }
      ui.input.focus();
      if (typeof ui.input.select === 'function') ui.input.select();
    };

    return ui;
  }

  var current = null;

  // Mount the panel inside ctx.mount. Returns true when it is in place and
  // false when the page gave us nothing to mount into (or anything threw);
  // the caller never needs a try block around this.
  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      injectStyle();
      var panel = createPanel(ctx);
      ctx.mount.appendChild(panel.root);
      current = panel;
      return true;
    } catch (err) {
      return false;
    }
  }

  // Put the cursor in the search box (the page binds the key). Returns false
  // before init or if the box could not take focus.
  function focus() {
    if (!current) return false;
    try { current.focus(); return true; } catch (err) { return false; }
  }

  global.SDASatSearch = {
    init: init,
    rank: rank,
    focus: focus
  };

  // Node only, for tests/test_globe_satsearch.py. Browsers have no `module`, so
  // the page sees nothing new and window.SDASatSearch stays { init, rank, focus }.
  if (typeof module !== 'undefined' && module && module.exports) {
    module.exports = { noradToInt: noradToInt, noradKey: noradKey };
  }
})(typeof window !== 'undefined' ? window : this);
