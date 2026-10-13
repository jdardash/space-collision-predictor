// Live video from the ISS for the WorldView globe: a sidebar list of public camera
// feeds, each opening a floating player over the globe when the user asks for it.
//
// Adapted from simplifaisoul/osiris on GitHub, file src/components/SpaceCam.tsx,
// commit 8edb376acff0d4bcbbf23964f44d40c67c5a6e27, MIT license.
// Copyright (c) 2026 simplifaisoul
// Taken from it: the three feed ids with their labels and descriptions, the
// youtube-nocookie embed parameters, the nominal orbit figures and the idea of
// a source link beside the player. Everything else (plain script instead of a
// React component, the floating frame, id validation, the ISS hooks) is new.
// The MIT permission notice is kept in THIRD_PARTY.md.
//
// Nothing is requested from YouTube until a feed is pressed: the panel is
// plain text and links, and the iframe is created on the click and removed on
// close so playback stops. The module has no network code of its own. The
// pure helpers (id check, URL builders) are exported for the Node tests in
// tests/test_globe_isscam.py; init() is the only part that touches the page.

(function (global) {
  'use strict';

  var ISS_NORAD_ID = 25544;
  var PANEL_TITLE = 'Live from the ISS';

  // Shown only when the page has no altitude for the ISS yet. The orbit period
  // is the donor's figure and does not move much.
  var FALLBACK_ALT_KM = 408;
  var ORBIT_MINUTES = 93;

  // YouTube ids are 11 characters of this alphabet. Anything else is refused
  // before it can reach a URL or an attribute.
  var VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
  var EMBED_BASE = 'https://www.youtube-nocookie.com/embed/';
  var WATCH_BASE = 'https://www.youtube.com/watch?v=';
  // Muted autoplay so the click that opened the frame is enough to start it.
  var EMBED_QUERY = '?autoplay=1&mute=1&playsinline=1&modestbranding=1&rel=0';
  var IFRAME_ALLOW = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
  var IFRAME_POLICY = 'strict-origin-when-cross-origin';

  // Pixels the floating frame keeps clear of the page's fixed chrome: the left
  // column (10 + 240 wide) plus a gap, the header, the right column (info panel
  // and event feed, 300 wide at 10) and the 64 px time bar plus a gap. `chrome`
  // is the frame's own title bar, footer and borders (28 + 26 + 2), which the
  // 16:9 player height has to leave room for.
  var LAYOUT = { left: 262, top: 60, right: 322, bottom: 72, chrome: 56 };

  // ---------------------------------------------------------------------
  // Feeds
  // ---------------------------------------------------------------------

  // The list is short on purpose. The donor tested eleven well-known "24/7 ISS"
  // streams and only these passed: the official NASA ids no longer play and
  // several popular mirrors refuse embedding. The last two share a channel, so
  // they can go dark together. Live ids rot, so every row also carries a plain
  // link to the stream's YouTube page. The array stays mutable so a test can add
  // a hostile entry; init() filters it again on every call.
  var FEEDS = [
    {
      id: 'sen-4k',
      label: '4K EARTH',
      detail: 'Sen, 4K cameras mounted on the ISS',
      source: 'Sen',
      videoId: 'fO9e9jnhYK8'
    },
    {
      id: 'iss-earth',
      label: 'EARTH VIEW',
      detail: 'ISS external camera, nadir',
      source: 'afarTV (re-broadcast)',
      videoId: 'tj4knR4r1UU'
    },
    {
      id: 'iss-overview',
      label: 'OVERVIEW CAM',
      detail: 'ISS overview camera, wide',
      source: 'afarTV (re-broadcast)',
      videoId: 'OKQEMp2555A'
    }
  ];

  function isValidVideoId(id) {
    // typeof first: the pattern would otherwise coerce an array or an object
    // with a toString to a string and wave it through.
    return typeof id === 'string' && VIDEO_ID.test(id);
  }

  function embedUrl(id) {
    return isValidVideoId(id) ? EMBED_BASE + id + EMBED_QUERY : null;
  }

  function watchUrl(id) {
    return isValidVideoId(id) ? WATCH_BASE + id : null;
  }

  // The feeds that can actually be opened: well-formed entries with a valid id.
  function usableFeeds() {
    var list = [];
    for (var i = 0; i < FEEDS.length; i++) {
      var f = FEEDS[i];
      if (!f || typeof f !== 'object' || typeof f.id !== 'string') continue;
      if (!isValidVideoId(f.videoId)) continue;
      list.push(f);
    }
    return list;
  }

  function textOr(value, fallback) {
    return (typeof value === 'string' && value) ? value : fallback;
  }

  // A NORAD id as a positive integer, or null. The page sends numbers; a digit
  // string is tolerated.
  function toNoradId(value) {
    var n = value;
    if (typeof n === 'string' && /^\d+$/.test(n)) n = parseInt(n, 10);
    if (typeof n !== 'number' || !isFinite(n) || n <= 0 || Math.floor(n) !== n) return null;
    return n;
  }

  // ---------------------------------------------------------------------
  // Style and DOM helpers
  // ---------------------------------------------------------------------

  function px(n) { return n + 'px'; }

  // The player width is the smallest of its nominal size, what is left between
  // the two columns, and what a 16:9 player can have without its bars reaching
  // the time bar.
  function frameWidth(nominal) {
    var across = LAYOUT.left + LAYOUT.right;
    var down = LAYOUT.top + LAYOUT.bottom + LAYOUT.chrome;
    return 'min(' + px(nominal) + ', calc(100vw - ' + px(across) + '), ' +
      'calc((100vh - ' + px(down) + ') * 16 / 9))';
  }

  var STYLE_ID = 'isscam-style';
  var STYLE_CSS = [
    '.mode-buttons.isscam-list { grid-template-columns: 1fr; }',
    '.isscam-row { display: flex; align-items: stretch; gap: 4px; }',
    '.mode-btn.isscam-open { flex: 1; min-width: 0; padding: 5px 8px; text-align: left; }',
    '.isscam-name { display: block; font-weight: 700; letter-spacing: 2px; }',
    '.isscam-detail { display: block; margin-top: 1px; font-size: 9px; font-weight: 400;',
    '  letter-spacing: 0.5px; color: var(--hud-dim, #7a5500); }',
    '.isscam-link { flex: 0 0 auto; align-self: center; padding: 0 2px; font-size: 9px;',
    '  color: var(--hud-dim, #7a5500); text-decoration: underline; }',
    '.isscam-link:hover { color: var(--hud-color, #ffb000); }',
    '.mode-btn.isscam-find { width: 100%; margin-top: 4px; padding: 6px 0; }',
    '.isscam-status, .isscam-note { margin-top: 6px; font-size: 9px; letter-spacing: 1px;',
    '  color: var(--hud-dim, #7a5500); }',
    '.isscam-status:empty { display: none; }',
    '.isscam-frame { position: fixed; left: ' + px(LAYOUT.left) + '; top: ' +
      px(LAYOUT.top) + ';',
    '  z-index: 45; box-sizing: border-box; width: ' + frameWidth(480) + '; min-width: 240px;',
    '  background: rgba(0,0,0,0.92); border: 1px solid var(--hud-color, #ffb000);',
    '  border-radius: 4px; overflow: hidden; font-size: 10px; letter-spacing: 1px;',
    '  color: var(--hud-color, #ffb000); }',
    '.isscam-frame.isscam-big { width: ' + frameWidth(960) + '; }',
    '.isscam-head { display: flex; align-items: center; gap: 6px; height: 28px;',
    '  box-sizing: border-box; padding: 0 8px; background: rgba(0,0,0,0.6);',
    '  border-bottom: 1px solid rgba(255,176,0,0.15); }',
    '.isscam-label { flex: 0 0 auto; font-weight: 700; letter-spacing: 2px;',
    '  white-space: nowrap; }',
    '.isscam-source { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;',
    '  white-space: nowrap; color: var(--hud-dim, #7a5500); }',
    '.isscam-btn { flex: 0 0 auto; background: none; border: 1px solid rgba(255,176,0,0.3);',
    '  color: var(--hud-color, #ffb000); padding: 2px 8px; font-size: 9px; font-family: inherit;',
    '  cursor: pointer; border-radius: 2px; }',
    '.isscam-btn:hover { border-color: var(--hud-color, #ffb000); }',
    '.isscam-video { position: relative; width: 100%; aspect-ratio: 16 / 9; background: #000; }',
    '.isscam-video iframe { position: absolute; left: 0; top: 0; width: 100%; height: 100%;',
    '  border: 0; }',
    '.isscam-foot { display: flex; align-items: center; gap: 8px; height: 26px;',
    '  box-sizing: border-box; padding: 0 8px; overflow: hidden; white-space: nowrap;',
    '  border-top: 1px solid rgba(255,176,0,0.15); }',
    '.isscam-caption { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;',
    '  color: var(--hud-dim, #7a5500); }',
    '.isscam-fallback { flex: 0 0 auto; color: var(--hud-color, #ffb000); }'
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

  function button(className, text) {
    var node = el('button', className, text);
    node.setAttribute('type', 'button');
    return node;
  }

  // A link to the stream's YouTube page in a new tab, or null for a bad id. It
  // is the fallback when an embed is dead or blocked, so it must work without
  // any iframe having been created.
  function youtubeLink(videoId, text, className) {
    var href = watchUrl(videoId);
    if (!href) return null;
    var a = el('a', className, text);
    a.setAttribute('href', href);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  // ---------------------------------------------------------------------
  // Panel
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  // One panel bound to one page context. Every call into the page sits in its
  // own try block: a failure there must not take the globe down.
  function createPanel(ctx) {
    var ui = { ctx: ctx, active: null, frame: null, player: null, buttons: [] };

    ui.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    ui.list = el('div', 'mode-buttons isscam-list');
    ui.find = button('mode-btn isscam-find', 'Find the ISS');
    ui.status = el('div', 'isscam-status');
    ui.status.setAttribute('role', 'status');
    var note = el('div', 'isscam-note',
      'Nothing is requested from YouTube until you press a feed.');
    ui.root.appendChild(head);
    ui.root.appendChild(body);
    body.appendChild(ui.list);
    body.appendChild(ui.find);
    body.appendChild(ui.status);
    body.appendChild(note);

    // The page binds a collapse toggle to every .panel h3 once at start-up.
    // Taking the click here first (and stopping it) gives one toggle whether
    // init runs before or after that binding.
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      ui.root.classList.toggle('collapsed');
    });

    ui.setStatus = function (message) { ui.status.textContent = message; };

    ui.buildRow = function (feed) {
      var row = el('div', 'isscam-row');
      var open = button('mode-btn isscam-open');
      open.appendChild(el('span', 'isscam-name', textOr(feed.label, feed.id)));
      open.appendChild(el('span', 'isscam-detail', textOr(feed.detail, '')));
      open.setAttribute('aria-pressed', 'false');
      open.addEventListener('click', function () {
        try {
          if (ui.active === feed.id) ui.close();
          else ui.open(feed);
        } catch (err) { /* keep going */ }
      });
      row.appendChild(open);
      var link = youtubeLink(feed.videoId, 'YouTube', 'isscam-link');
      if (link) {
        link.setAttribute('title', 'Open this stream on YouTube in a new tab');
        row.appendChild(link);
      }
      ui.buttons.push({ id: feed.id, node: open });
      return row;
    };

    ui.markPressed = function () {
      for (var i = 0; i < ui.buttons.length; i++) {
        var on = (ui.buttons[i].id === ui.active);
        ui.buttons[i].node.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on) ui.buttons[i].node.classList.add('active');
        else ui.buttons[i].node.classList.remove('active');
      }
    };

    // The altitude the globe shows for the ISS, or null before it is known.
    ui.issRecord = function () {
      try {
        var map = ui.ctx.satEngine.byNoradId;
        return (map && map[ISS_NORAD_ID]) || null;
      } catch (err) {
        return null;
      }
    };

    ui.caption = function () {
      var rec = ui.issRecord();
      var alt = (rec && isNum(rec.lastAlt) && rec.lastAlt > 0) ? Math.round(rec.lastAlt) : null;
      var left = alt === null ? 'ALT ~' + FALLBACK_ALT_KM : 'ALT ' + alt;
      return left + ' KM | ORBIT ~' + ORBIT_MINUTES + ' MIN';
    };

    // The frame is a child of <body>, not of the sidebar: the left column
    // scrolls and clips, and a fixed child would move with it on some browsers.
    ui.buildFrame = function () {
      var root = el('div', 'isscam-frame');
      root.setAttribute('role', 'dialog');
      root.setAttribute('aria-label', 'Live video from the ISS');
      var bar = el('div', 'isscam-head');
      ui.labelEl = el('span', 'isscam-label');
      ui.sourceEl = el('span', 'isscam-source');
      ui.sizeBtn = button('isscam-btn isscam-size', 'Enlarge');
      var closeBtn = button('isscam-btn isscam-close', 'Close');
      closeBtn.setAttribute('aria-label', 'Close live video');
      bar.appendChild(ui.labelEl);
      bar.appendChild(ui.sourceEl);
      bar.appendChild(ui.sizeBtn);
      bar.appendChild(closeBtn);
      ui.videoBox = el('div', 'isscam-video');
      ui.foot = el('div', 'isscam-foot');
      root.appendChild(bar);
      root.appendChild(ui.videoBox);
      root.appendChild(ui.foot);

      // A bigger player is the only way to get a sharper stream, because the
      // rendered size picks the resolution. Resizing with CSS keeps the same
      // iframe, so the stream does not restart.
      ui.sizeBtn.addEventListener('click', function () {
        var big = !root.classList.contains('isscam-big');
        if (big) root.classList.add('isscam-big');
        else root.classList.remove('isscam-big');
        ui.sizeBtn.textContent = big ? 'Shrink' : 'Enlarge';
      });
      closeBtn.addEventListener('click', function () { ui.close(); });
      root.addEventListener('keydown', function (e) {
        if (e && e.key === 'Escape') ui.close();
      });
      global.document.body.appendChild(root);
      return root;
    };

    ui.open = function (feed) {
      var url = embedUrl(feed.videoId);
      if (!url) return false;
      if (!ui.frame) ui.frame = ui.buildFrame();

      // A new element each time, so the old stream is gone before the new one
      // starts rather than navigated in place.
      ui.dropPlayer();
      var name = textOr(feed.label, feed.id);
      var player = el('iframe');
      player.setAttribute('src', url);
      player.setAttribute('title', 'ISS live video, ' + name);
      player.setAttribute('allow', IFRAME_ALLOW);
      player.setAttribute('referrerpolicy', IFRAME_POLICY);
      ui.videoBox.appendChild(player);
      ui.player = player;

      ui.labelEl.textContent = name;
      ui.labelEl.setAttribute('title', textOr(feed.detail, name));
      ui.sourceEl.textContent = 'Source: ' + textOr(feed.source, 'YouTube') + ', via YouTube';
      // The frame cannot tell a dead stream from a slow one (the iframe is
      // cross-origin), so the way out is always on screen.
      ui.foot.textContent = '';
      ui.foot.appendChild(el('span', 'isscam-caption', ui.caption()));
      var fallback = youtubeLink(feed.videoId, 'Not playing? Open on YouTube', 'isscam-fallback');
      if (fallback) ui.foot.appendChild(fallback);

      ui.active = feed.id;
      ui.markPressed();
      return true;
    };

    ui.dropPlayer = function () {
      var player = ui.player;
      ui.player = null;
      if (player && player.parentNode) {
        try { player.parentNode.removeChild(player); } catch (err) { /* keep going */ }
      }
    };

    // Take the iframe out of the document first: removing only the frame
    // would leave the player attached to a detached subtree.
    ui.close = function () {
      var frame = ui.frame;
      ui.frame = null;
      ui.active = null;
      ui.dropPlayer();
      if (frame && frame.parentNode) {
        try { frame.parentNode.removeChild(frame); } catch (err) { /* keep going */ }
      }
      ui.markPressed();
    };

    ui.findIss = function () {
      var rec = ui.issRecord();
      if (!rec) {
        ui.setStatus('The ISS (NORAD ' + ISS_NORAD_ID + ') is not in the loaded catalog.');
        return false;
      }
      try {
        ui.ctx.selectSatellite(ISS_NORAD_ID);
      } catch (err) {
        ui.setStatus('The page could not select the ISS.');
        return false;
      }
      // The page announces the selection itself (sda:satellite-selected), so
      // nothing is written to the event feed from here.
      ui.setStatus('Selected ' + textOr(rec.name, 'the ISS') + '.');
      return true;
    };
    ui.find.addEventListener('click', function () {
      try { ui.findIss(); } catch (err) { /* keep going */ }
    });

    var feeds = usableFeeds();
    for (var i = 0; i < feeds.length; i++) ui.list.appendChild(ui.buildRow(feeds[i]));
    if (!feeds.length) ui.setStatus('No video feeds are configured.');

    return ui;
  }

  // ---------------------------------------------------------------------
  // Page hooks
  // ---------------------------------------------------------------------

  var current = null;
  var listening = false;

  // One document listener for the page's lifetime, routed to the newest panel,
  // so a second init never doubles the event-feed line.
  function onSelected(e) {
    if (!current) return;
    try {
      var detail = e && e.detail;
      if (!detail || toNoradId(detail.noradId) !== ISS_NORAD_ID) return;
      current.ctx.addIntelItem('ISS selected, live video from its cameras is in the "' +
        PANEL_TITLE + '" panel (left column).');
    } catch (err) {
      /* the globe must keep working */
    }
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
      if (current) current.close();
      current = panel;
      if (!listening) {
        global.document.addEventListener('sda:satellite-selected', onSelected);
        listening = true;
      }
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAIssCam = {
    init: init,
    FEEDS: FEEDS,
    ISS_NORAD_ID: ISS_NORAD_ID,
    LAYOUT: LAYOUT,
    isValidVideoId: isValidVideoId,
    embedUrl: embedUrl,
    watchUrl: watchUrl
  };
})(typeof window !== 'undefined' ? window : this);
