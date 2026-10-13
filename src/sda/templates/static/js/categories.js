// Constellation filter for the WorldView globe: pills with live counts that hide
// every satellite outside the chosen constellations.
//
// Adapted from PremaanshVyas/satlas (https://github.com/PremaanshVyas/satlas),
// MIT License, Copyright (c) 2026 Premaansh Vyas, commit
// 410f9238e54859e93ac46e93cc2e626fcfa53bcb:
//   apps/web/src/globe/Globe.ts                       classifySatellite() and SatCategory
//   apps/web/src/components/MobileControlsSheet.tsx   the category pills and their labels
// From the donor: the name-prefix classifier (Starlink, GPS and Iridium prefixes,
// the NAVSTAR substring, the debris markers) and the Starlink / GPS / Iridium /
// Debris / Other category set with its labels. The donor's pills are a multi-select
// toggle over a Set; that is kept. Everything marked "ours" below is new here. The
// full permission notice is in THIRD_PARTY.md.
//
// classify() and tally() are pure (no DOM), so they run under Node in
// tests/test_globe_categories.py. init() builds one sidebar panel inside the element
// the page hands it. The page owns the dots: this module only sets record.hidden and
// asks the page to apply it through ctx.refreshSatVisibility().

(function (global) {
  'use strict';

  // Category order is the pill order, and it never changes, so the row does not
  // reshuffle when the catalog reloads. Keys marked (ours) are not in the donor.
  var CATEGORIES = [
    { key: 'STARLINK', label: 'Starlink' },
    { key: 'ONEWEB', label: 'OneWeb' },                 // ours
    { key: 'KUIPER', label: 'Kuiper' },                 // ours
    { key: 'CHINA_MEGA', label: 'Qianfan/Guowang' },    // ours
    { key: 'GPS', label: 'GPS' },
    { key: 'GLONASS', label: 'GLONASS' },               // ours
    { key: 'GALILEO', label: 'Galileo' },               // ours
    { key: 'BEIDOU', label: 'BeiDou' },                 // ours
    { key: 'IRIDIUM', label: 'Iridium' },
    { key: 'STATIONS', label: 'Stations' },             // ours
    { key: 'WEATHER', label: 'Weather' },               // ours
    { key: 'GEO', label: 'Geostationary' },             // ours
    { key: 'DEBRIS', label: 'Debris' },
    { key: 'OTHER', label: 'Other' }
  ];

  var INDEX = {};
  for (var ci = 0; ci < CATEGORIES.length; ci++) INDEX[CATEGORIES[ci].key] = ci;

  // A record that is not an object (the array can hold junk) gets this marker in
  // the per-record category array and is never written to.
  var SKIP = 255;

  // One RegExp from several literals, so a long rule can be written one clause
  // per line without a string-escaped pattern.
  function anyOf() {
    var parts = [];
    for (var i = 0; i < arguments.length; i++) parts.push(arguments[i].source);
    return new RegExp(parts.join('|'));
  }

  // First match wins, so the order is load-bearing. Names are upper-cased once
  // before the rules run.
  var RULES = [
    // ours, and placed first on purpose: the donor tests Starlink, GPS and Iridium
    // before debris, which files "IRIDIUM 33 DEB" under Iridium and would file
    // "FENGYUN 1C DEB" under Weather if that rule came first. The donor's bare
    // ' DEB' substring is tightened to the whole word so "DEBUT" is not debris.
    // "ISS OBJECT YM" is a catalogued fragment released from the station.
    ['DEBRIS', /(?:^|\s)DEB(?:\s|$)|DEBRIS|R\/B|ROCKET BODY|^ISS OBJECT/],
    // donor: startsWith('STARLINK'). Also covers "STARLINK-1234 [DTC]".
    ['STARLINK', /^STARLINK/],
    ['ONEWEB', /^ONEWEB/],                              // ours
    ['KUIPER', /^KUIPER/],                              // ours
    // ours: Qianfan (Thousand Sails) and Guowang, which CelesTrak lists as
    // "HULIANWANG DIGUI-n" plus the "HULIANWANG JISHU SHIYAN*" test satellites.
    ['CHINA_MEGA', /^(?:QIANFAN|HULIANWANG)/],
    // donor: GPS / BIIF / BIII prefixes and the NAVSTAR substring, unchanged.
    ['GPS', /^(?:GPS|BIIF|BIII)|NAVSTAR/],
    ['GLONASS', /GLONASS/],                             // ours: "COSMOS 2545 (GLONASS-M)"
    // ours: Galileo satellites are "GSAT0101 (GALILEO-PFM)"; the zero after GSAT
    // keeps the Indian GEO series "GSAT-15" out.
    ['GALILEO', /^GSAT0\d|GALILEO/],
    ['BEIDOU', /BEIDOU/],                               // ours
    ['IRIDIUM', /^IRIDIUM/],                            // donor: startsWith('IRIDIUM')
    // ours: the two stations (named "ISS (ZARYA)", "CSS (TIANHE)" per module) and
    // the visiting vehicles the active catalog carries while they are docked.
    ['STATIONS', anyOf(
      /^(?:ISS|CSS)\s*\(/,
      /^(?:TIANGONG|CREW DRAGON|SOYUZ-MS|PROGRESS-MS|SHENZHOU|TIANZHOU|CYGNUS|HTV-X)/)],
    // ours: operational weather satellites. "(GOES" catches "EWS-G2 (GOES 15)".
    ['WEATHER', anyOf(
      /^(?:NOAA|GOES|EWS-G|METEOSAT|METOP|FENGYUN|METEOR|HIMAWARI|DMSP|SUOMI NPP)/,
      /^(?:JPSS|ELEKTRO-L|INSAT-3D|FY-)/,
      /\(GOES/)]
  ].map(function (r) { return { idx: INDEX[r[0]], re: r[1] }; });

  // ---------------------------------------------------------------------
  // Classification (pure)
  // ---------------------------------------------------------------------

  // Category index for one name. The regime is a last resort (ours): a
  // geostationary object that no name rule claimed is a geostationary comsat.
  // The regime comes from the server's own labelling, so it is only consulted
  // when the name says nothing.
  function classifyIndex(name, regime) {
    var n = (typeof name === 'string') ? name.toUpperCase() : '';
    if (n) {
      for (var i = 0; i < RULES.length; i++) {
        if (RULES[i].re.test(n)) return RULES[i].idx;
      }
    }
    return regime === 'GEO' ? INDEX.GEO : INDEX.OTHER;
  }

  function classify(name, regime) {
    return CATEGORIES[classifyIndex(name, regime)].key;
  }

  // Classify a whole record array once. `cats` is aligned with `sats` by index
  // (SKIP for entries that are not objects) so a pill click never has to look
  // at a name again; `counts` is keyed by category key.
  function tally(sats) {
    var n = (sats && typeof sats.length === 'number') ? sats.length : 0;
    var cats = new Uint8Array(n);
    var perIndex = new Array(CATEGORIES.length);
    for (var k = 0; k < perIndex.length; k++) perIndex[k] = 0;
    var total = 0;
    for (var i = 0; i < n; i++) {
      var rec = sats[i];
      if (!rec || typeof rec !== 'object') { cats[i] = SKIP; continue; }
      var c = classifyIndex(rec.name, rec.regime);
      cats[i] = c;
      perIndex[c]++;
      total++;
    }
    var counts = {};
    for (var j = 0; j < CATEGORIES.length; j++) counts[CATEGORIES[j].key] = perIndex[j];
    return { total: total, counts: counts, cats: cats };
  }

  // ---------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------

  // Thousands separators without toLocaleString, whose output depends on the
  // browser's locale.
  function fmt(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function displayName(rec) {
    if (rec && typeof rec.name === 'string' && rec.name.trim()) return rec.name;
    return (rec && rec.noradId != null) ? 'NORAD ' + rec.noradId : '(unnamed)';
  }

  var STYLE_ID = 'categories-style';
  // The pills reuse the page's .mode-btn look (and its .active state); these rules
  // only lay them out as a wrapping row and add the disabled and count styling.
  var STYLE_CSS = [
    '.catf-pills { display: flex; flex-wrap: wrap; gap: 4px; }',
    '.catf-pill { width: auto; padding: 4px 7px; font-size: 9px; letter-spacing: 0.5px;',
    '  white-space: nowrap; }',
    '.catf-count { margin-left: 5px; opacity: 0.6; }',
    '.catf-pill.active .catf-count { opacity: 1; }',
    '.catf-pill.catf-disabled { opacity: 0.35; cursor: default; }',
    '.catf-pill.catf-disabled:hover { background: rgba(255,176,0,0.05);',
    '  border-color: rgba(255,176,0,0.15); }',
    '.catf-note { margin-top: 6px; font-size: 9px; letter-spacing: 1px;',
    '  color: var(--hud-dim, #7a5500); overflow-wrap: anywhere; }',
    '.catf-note:empty { display: none; }'
  ].join('\n');

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

  // One filter bound to one page context. Every call into the page sits in its
  // own try block: a failure there must not take the globe down or stop the
  // other steps.
  function createPanel(ctx) {
    var ui = {
      ctx: ctx,
      pills: [],                                    // { key, idx, btn, countEl }
      mask: new Uint8Array(CATEGORIES.length),      // 1 = category is in the set
      nSel: 0,                                      // how many categories are in the set
      snap: { arr: null, n: 0, cats: new Uint8Array(0) },
      counts: {},
      total: 0,
      forced: null,                                 // record shown only because it is selected
      forcedCat: -1
    };

    ui.root = el('div', 'panel');
    var body = el('div', 'panel-body');
    ui.row = el('div', 'catf-pills');
    ui.note = el('div', 'catf-note');
    ui.root.appendChild(el('h3', null, 'Constellation Filter'));
    ui.root.appendChild(body);
    body.appendChild(ui.row);
    body.appendChild(ui.note);

    function addPill(key, idx, label) {
      var btn = el('button', 'mode-btn catf-pill');
      btn.setAttribute('type', 'button');
      btn.setAttribute('data-cat', key);
      btn.setAttribute('aria-pressed', 'false');
      btn.appendChild(el('span', 'catf-name', label));
      var countEl = el('span', 'catf-count', '0');
      btn.appendChild(countEl);
      btn.addEventListener('click', function () {
        try { ui.onPill(idx); } catch (err) { /* the globe must keep working */ }
      });
      ui.row.appendChild(btn);
      ui.pills.push({ key: key, idx: idx, btn: btn, countEl: countEl });
    }
    addPill('ALL', -1, 'All');
    for (var i = 0; i < CATEGORIES.length; i++) addPill(CATEGORIES[i].key, i, CATEGORIES[i].label);

    // The page replaces satEngine.sats on every reload, so read it each time.
    ui.catalog = function () {
      try {
        var sats = ui.ctx.satEngine.sats;
        return (sats && typeof sats.length === 'number') ? sats : [];
      } catch (err) {
        return [];
      }
    };

    ui.selectedId = function () {
      try {
        var id = ui.ctx.satEngine.selectedId;
        return id == null ? null : id;
      } catch (err) {
        return null;
      }
    };

    ui.lookup = function (id) {
      try {
        var rec = ui.ctx.satEngine.byNoradId[id];
        return (rec && typeof rec === 'object') ? rec : null;
      } catch (err) {
        return null;
      }
    };

    // Classify the array once and remember the result. A selected category that
    // no longer has members is dropped: keeping it would hide every dot.
    ui.load = function (sats) {
      var t = tally(sats);
      ui.snap = { arr: sats, n: sats.length, cats: t.cats };
      ui.counts = t.counts;
      ui.total = t.total;
      for (var i = 0; i < CATEGORIES.length; i++) {
        if (ui.mask[i] && t.counts[CATEGORIES[i].key] === 0) {
          ui.mask[i] = 0;
          ui.nSel--;
        }
      }
    };

    // The page could swap or grow the array without telling us; classify again
    // then, once, instead of trusting an index that no longer lines up.
    ui.sync = function () {
      var sats = ui.catalog();
      if (sats !== ui.snap.arr || sats.length !== ui.snap.n) ui.load(sats);
    };

    // The only per-record work a click does: one boolean write each, from the
    // category array built at load time.
    ui.applyFlags = function () {
      var sats = ui.snap.arr;
      var cats = ui.snap.cats;
      var n = ui.snap.n;
      var mask = ui.mask;
      var showAll = ui.nSel === 0;
      for (var i = 0; i < n; i++) {
        var c = cats[i];
        if (c === SKIP) continue;
        sats[i].hidden = showAll ? false : mask[c] === 0;
      }
      ui.forced = null;
      ui.forcedCat = -1;
      if (showAll) return;
      // A hidden category must never make the selected satellite invisible.
      var sel = ui.selectedId();
      var rec = sel === null ? null : ui.lookup(sel);
      if (rec && rec.hidden === true) {
        rec.hidden = false;
        ui.forced = rec;
        ui.forcedCat = classifyIndex(rec.name, rec.regime);
      }
    };

    ui.shown = function () {
      if (ui.nSel === 0) return ui.total;
      var n = 0;
      for (var i = 0; i < CATEGORIES.length; i++) {
        if (ui.mask[i]) n += ui.counts[CATEGORIES[i].key];
      }
      return n;
    };

    ui.paint = function () {
      for (var i = 0; i < ui.pills.length; i++) {
        var p = ui.pills[i];
        var isAll = p.idx < 0;
        var on = isAll ? ui.nSel === 0 : ui.mask[p.idx] === 1;
        var count = isAll ? ui.total : ui.counts[p.key] || 0;
        var empty = !isAll && count === 0;
        p.countEl.textContent = fmt(count);
        p.btn.classList.toggle('active', on);
        p.btn.classList.toggle('catf-disabled', empty);
        p.btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        p.btn.setAttribute('aria-disabled', empty ? 'true' : 'false');
        p.btn.disabled = empty;
      }
      var text = ui.nSel === 0
        ? 'Showing all ' + fmt(ui.total) + ' satellites.'
        : 'Showing ' + fmt(ui.shown()) + ' of ' + fmt(ui.total) + ' satellites.';
      if (ui.forced) text += ' Selected stays visible: ' + displayName(ui.forced) + '.';
      ui.note.textContent = text;
    };

    ui.refresh = function () {
      try { ui.ctx.refreshSatVisibility(); } catch (err) { /* flags are written already */ }
    };

    ui.log = function (text) {
      try { ui.ctx.addIntelItem(text); } catch (err) { /* keep going */ }
    };

    ui.describe = function () {
      if (ui.nSel === 0) return 'Filter cleared, showing all ' + fmt(ui.total) + ' satellites';
      var names = [];
      for (var i = 0; i < CATEGORIES.length; i++) {
        if (ui.mask[i]) names.push(CATEGORIES[i].label);
      }
      return 'Filter: ' + names.join(', ') + ' (' + fmt(ui.shown()) + ' of ' +
        fmt(ui.total) + ' shown)';
    };

    // idx -1 is the All pill. A pill with no members does nothing.
    ui.onPill = function (idx) {
      ui.sync();
      if (idx < 0) {
        ui.mask.fill(0);
        ui.nSel = 0;
      } else {
        if (!ui.mask[idx] && !ui.counts[CATEGORIES[idx].key]) return;
        if (ui.mask[idx]) { ui.mask[idx] = 0; ui.nSel--; } else { ui.mask[idx] = 1; ui.nSel++; }
      }
      ui.applyFlags();
      ui.paint();
      ui.refresh();
      ui.log(ui.describe());
    };

    // The record array was replaced: recount, then put the filter on the new
    // records. With no filter set there is nothing to write (new records start
    // visible) and nothing for the page to refresh.
    ui.onLoaded = function () {
      ui.load(ui.catalog());
      ui.forced = null;
      ui.forcedCat = -1;
      if (ui.nSel > 0) {
        ui.applyFlags();
        ui.paint();
        ui.refresh();
      } else {
        ui.paint();
      }
    };

    // Selecting a satellite the filter hides shows that one record. The one
    // shown for the previous selection goes back under the filter. The event's
    // id is the authority here: satEngine.selectedId is not guaranteed to be
    // set yet when a listener runs.
    ui.onSelected = function (e) {
      var d = e && e.detail;
      if (!d || d.noradId == null || ui.nSel === 0) return;
      var rec = ui.lookup(d.noradId);
      if (!rec) return;
      var changed = false;
      if (ui.forced && ui.forced !== rec && ui.mask[ui.forcedCat] === 0) {
        ui.forced.hidden = true;
        changed = true;
      }
      if (ui.forced !== rec) {
        ui.forced = null;
        ui.forcedCat = -1;
      }
      if (!ui.forced && rec.hidden === true) {
        rec.hidden = false;
        ui.forced = rec;
        ui.forcedCat = classifyIndex(rec.name, rec.regime);
        changed = true;
      }
      if (!changed) return;
      ui.paint();
      ui.refresh();
    };

    // The selection was cleared. The record that was shown only because it was
    // selected goes back under the filter. The page fires this before the
    // 'selected' event of the next pick, so the new one is shown by onSelected
    // afterwards, and re-selecting this one shows it again. The event's id is
    // optional; when it names a different record than the one forced, the
    // events arrived out of order and the forced (newer) record is left alone.
    ui.onDeselected = function (e) {
      if (!ui.forced) return;
      var d = e && e.detail;
      if (d && d.noradId != null) {
        var named = ui.lookup(d.noradId);
        if (named && named !== ui.forced) return;
      }
      var hide = ui.mask[ui.forcedCat] === 0;
      if (hide) ui.forced.hidden = true;
      ui.forced = null;
      ui.forcedCat = -1;
      ui.paint();
      if (hide) ui.refresh();
    };

    ui.load(ui.catalog());
    ui.paint();
    return ui;
  }

  var current = null;

  function detach(panel) {
    var doc = global.document;
    if (!panel || !doc || typeof doc.removeEventListener !== 'function') return;
    doc.removeEventListener('sda:catalog-loaded', panel.loadedHandler);
    doc.removeEventListener('sda:satellite-selected', panel.selectedHandler);
    doc.removeEventListener('sda:satellite-deselected', panel.deselectedHandler);
  }

  // Mount the panel inside ctx.mount. Returns true when it is in place and
  // false when the page gave us nothing to mount into (or anything threw); the
  // caller never needs a try block around this.
  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      injectStyle();
      var panel = createPanel(ctx);
      ctx.mount.appendChild(panel.root);
      // A second init takes over: the old panel stops listening instead of
      // filtering a catalog it no longer owns.
      detach(current);
      panel.loadedHandler = function (e) {
        try { panel.onLoaded(e); } catch (err) { /* keep going */ }
      };
      panel.selectedHandler = function (e) {
        try { panel.onSelected(e); } catch (err) { /* keep going */ }
      };
      panel.deselectedHandler = function (e) {
        try { panel.onDeselected(e); } catch (err) { /* keep going */ }
      };
      global.document.addEventListener('sda:catalog-loaded', panel.loadedHandler);
      global.document.addEventListener('sda:satellite-selected', panel.selectedHandler);
      global.document.addEventListener('sda:satellite-deselected', panel.deselectedHandler);
      current = panel;
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDACategories = {
    init: init,
    classify: classify,
    categories: CATEGORIES,
    tally: tally
  };
})(typeof window !== 'undefined' ? window : this);
