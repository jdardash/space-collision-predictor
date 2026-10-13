// World data feeds for the WorldView globe: one toggle per server proxy, each
// drawing into its own Cesium.CustomDataSource, plus the pure parsing and
// selection helpers behind them.
//
// The one-registry-entry-per-feed shape follows gods-eye-view
// (https://github.com/bilawalsidhu/gods-eye-view, MIT license, Copyright (c)
// 2026 Bilawal Sidhu), the same project tactical.js credits.
//
// Frames: everything here is geodetic degrees (latitude, longitude). Nothing in
// this file touches an inertial frame; orbital state vectors stay on the server
// and in sgp4-worker.js.
//
// No API key ever reaches this file. The keyed feeds (fires, AIS) are fetched
// from same-origin proxies that read the key from the server environment; the
// proxy only reports whether a key is configured, and the panel says so.
//
// Day and night shading is not done here: the page uses Cesium's own globe
// lighting for that.
//
// Page wiring (see init): the page loads this file after tactical.js and calls
// SDALayers.init(ctx) once the globe is up.

(function (window) {
  'use strict';

  var DEG = Math.PI / 180.0;
  var EARTH_RADIUS_KM = 6378.135; // WGS72 equatorial radius (sda.constants)

  // A feed is never fetched more often than this, whatever triggers it.
  var MIN_REFRESH_S = 300;
  var DEFAULT_CAP = 500;
  var TICK_MS = 30000;
  var BBOX_SPAN_DEG = 5.0;      // the military proxy refuses spans above 6
  var DEFAULT_CENTER = { lat: 20.0, lon: 0.0 }; // the page's opening view

  // ---------------------------------------------------------------------
  // Registry
  // ---------------------------------------------------------------------
  //
  // key        toggle id suffix
  // label      panel text
  // endpoint   same-origin proxy path
  // kind       'points' draws on the globe; 'panel' shows values in the panel
  // listKey    the array in the proxy response that holds the records
  // type       entity type string handed to the page's pick handler
  // params     how the request is built: none, camera (lat/lon), bbox, city
  // select     how the cap picks records: 'nearest' to the view centre, or
  //            'rank' (highest rank first, unranked last)
  // keyEnv     server environment variable the proxy needs, or ''
  // keyHint    where the free key comes from
  // refreshS   automatic refresh interval in seconds (0 = never; the Refresh
  //            button still works). Never below MIN_REFRESH_S when non-zero.

  var FEEDS = [
    {
      key: 'launches', label: 'Launches', endpoint: '/api/launches', kind: 'points',
      listKey: 'launches', type: 'launch', params: 'none', select: 'nearest',
      color: '#4ea3ff', size: 9, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'Launch Library 2 (The Space Devs)', refreshS: 900
    },
    {
      key: 'spaceweather', label: 'Space weather', endpoint: '/api/space-weather',
      kind: 'panel', listKey: '', type: 'space weather', params: 'none', select: 'rank',
      color: '#ffffff', size: 0, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'NOAA Space Weather Prediction Center', refreshS: 300
    },
    {
      key: 'volcanoes', label: 'Volcanoes', endpoint: '/api/volcanoes', kind: 'points',
      listKey: 'volcanoes', type: 'volcano', params: 'none', select: 'rank',
      color: '#ff6a3d', size: 6, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'Smithsonian Global Volcanism Program', refreshS: 0
    },
    {
      key: 'weatheralerts', label: 'Weather alerts (US)', endpoint: '/api/weather-alerts',
      kind: 'points', listKey: 'alerts', type: 'weather alert', params: 'none',
      select: 'rank', color: '#ffd24e', size: 8, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'NOAA National Weather Service', refreshS: 300
    },
    {
      key: 'gdelt', label: 'News mentions', endpoint: '/api/gdelt', kind: 'points',
      listKey: 'events', type: 'news event', params: 'none', select: 'rank',
      color: '#c58bff', size: 6, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'GDELT Project GEO 2.1 (query: satellite)', refreshS: 600
    },
    {
      key: 'radio', label: 'Radio stations', endpoint: '/api/radio', kind: 'points',
      listKey: 'stations', type: 'radio station', params: 'camera', select: 'nearest',
      color: '#66e0a3', size: 6, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'Radio Browser, near the view centre', refreshS: 0
    },
    {
      key: 'bikeshare', label: 'Bikeshare', endpoint: '/api/bikeshare', kind: 'points',
      listKey: 'stations', type: 'bikeshare station', params: 'city', select: 'nearest',
      color: '#3fd0d4', size: 5, cap: DEFAULT_CAP, keyEnv: '', keyHint: '',
      source: 'GBFS operator feed, city nearest the view centre', refreshS: 300
    },
    {
      key: 'military', label: 'Military sites', endpoint: '/api/military-sites',
      kind: 'points', listKey: 'sites', type: 'military site', params: 'bbox',
      select: 'nearest', color: '#ff4d6d', size: 6, cap: DEFAULT_CAP, keyEnv: '',
      keyHint: '', source: 'OpenStreetMap contributors (ODbL) via Overpass, around the view centre',
      refreshS: 0
    },
    {
      key: 'fires', label: 'Active fires', endpoint: '/api/fires', kind: 'points',
      listKey: 'fires', type: 'fire', params: 'none', select: 'rank',
      color: '#ff8800', size: 5, cap: DEFAULT_CAP, keyEnv: 'FIRMS_MAP_KEY',
      keyHint: 'get one at firms.modaps.eosdis.nasa.gov/api/map_key',
      source: 'NASA FIRMS', refreshS: 600
    },
    {
      key: 'ais', label: 'Ships (AIS)', endpoint: '/api/ais', kind: 'points',
      listKey: 'vessels', type: 'vessel', params: 'none', select: 'nearest',
      color: '#7fd1ff', size: 5, cap: DEFAULT_CAP, keyEnv: 'AISSTREAM_API_KEY',
      keyHint: 'get one at aisstream.io', source: 'AISStream', refreshS: 300
    }
  ];

  // Keys match _GBFS_CITIES in sda/routes/worldview.py (a test compares them).
  var CITIES = {
    nyc: { label: 'New York', lat: 40.71, lon: -74.00 },
    dc: { label: 'Washington DC', lat: 38.90, lon: -77.04 },
    chicago: { label: 'Chicago', lat: 41.88, lon: -87.63 },
    boston: { label: 'Boston', lat: 42.36, lon: -71.06 },
    sf: { label: 'San Francisco', lat: 37.77, lon: -122.42 }
  };

  function feedByKey(key) {
    for (var i = 0; i < FEEDS.length; i++) {
      if (FEEDS[i].key === key) return FEEDS[i];
    }
    return null;
  }

  function attributionLine(key) {
    var feed = feedByKey(key);
    return feed ? feed.label + ': ' + feed.source : '';
  }

  // ---------------------------------------------------------------------
  // Small pure helpers
  // ---------------------------------------------------------------------

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // A coordinate from untrusted JSON: a finite number, or a plain decimal
  // string, within +/- limit. Anything else (null, '', true, [], NaN) is null.
  function toCoord(value, limit) {
    var n;
    if (typeof value === 'number') {
      n = value;
    } else if (typeof value === 'string') {
      var s = value.trim();
      if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)) return null;
      n = Number(s);
    } else {
      return null;
    }
    if (!isFinite(n) || Math.abs(n) > limit) return null;
    return n;
  }

  // Display text from untrusted input: control characters removed, runs of
  // whitespace collapsed, long values cut with an ellipsis. Markup is left as
  // literal characters because every sink in this file uses textContent.
  function cleanText(value, max) {
    if (typeof value === 'number') {
      if (!isFinite(value)) return '';
      value = String(value);
    }
    if (typeof value !== 'string') return '';
    var s = value.replace(/[\t\n\r]+/g, ' ').replace(/[\u0000-\u001f\u007f]+/g, '');
    s = s.replace(/ {2,}/g, ' ').trim();
    if (max && s.length > max) s = s.slice(0, Math.max(0, max - 3)) + '...';
    return s;
  }

  function formatInt(n) {
    var v = Math.round(Number(n) || 0);
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function formatClock(date) {
    return pad2(date.getHours()) + ':' + pad2(date.getMinutes());
  }

  function formatCountdown(netIso, nowMs) {
    var t = Date.parse(netIso);
    if (!isFinite(t)) return '';
    var s = Math.floor((t - nowMs) / 1000);
    if (s < 0) return 'past';
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d > 0) return 'in ' + d + 'd ' + pad2(h) + 'h';
    if (h > 0) return 'in ' + h + 'h ' + pad2(m) + 'm';
    return 'in ' + m + 'm';
  }

  function round1(n) { return String(Math.round(n * 10) / 10); }

  function finiteOrNull(v) {
    return (typeof v === 'number' && isFinite(v)) ? v : null;
  }

  // ---------------------------------------------------------------------
  // Geometry for request parameters and the cap
  // ---------------------------------------------------------------------

  function haversineKm(lat1, lon1, lat2, lon2) {
    var p1 = lat1 * DEG;
    var p2 = lat2 * DEG;
    var dp = p2 - p1;
    var dl = (lon2 - lon1) * DEG;
    var a = Math.sin(dp / 2) * Math.sin(dp / 2) +
      Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  // A box of at most spanDeg per side around a point, clamped to the globe so
  // south < north and west < east always hold.
  function bboxAround(lat, lon, spanDeg) {
    var half = spanDeg / 2;
    return {
      south: Math.max(-90, lat - half),
      west: Math.max(-180, lon - half),
      north: Math.min(90, lat + half),
      east: Math.min(180, lon + half)
    };
  }

  function nearestCity(lat, lon) {
    var best = null;
    var bestKm = Infinity;
    for (var key in CITIES) {
      if (!Object.prototype.hasOwnProperty.call(CITIES, key)) continue;
      var km = haversineKm(lat, lon, CITIES[key].lat, CITIES[key].lon);
      if (km < bestKm) { bestKm = km; best = key; }
    }
    return best;
  }

  // Mean of a polygon's outer-ring vertices (the closing vertex is dropped),
  // shifted across the antimeridian when the ring spans it. Returns null when
  // no ring has three usable vertices.
  function ringCentroid(rings) {
    if (!Array.isArray(rings)) return null;
    for (var r = 0; r < rings.length; r++) {
      var flat = rings[r];
      if (!Array.isArray(flat)) continue;
      var lons = [];
      var lats = [];
      for (var i = 0; i + 1 < flat.length; i += 2) {
        var lon = toCoord(flat[i], 180);
        var lat = toCoord(flat[i + 1], 90);
        if (lon === null || lat === null) continue;
        lons.push(lon);
        lats.push(lat);
      }
      var n = lons.length;
      if (n > 1 && lons[0] === lons[n - 1] && lats[0] === lats[n - 1]) {
        lons.pop();
        lats.pop();
        n--;
      }
      if (n < 3) continue;
      var wrap = (Math.max.apply(null, lons) - Math.min.apply(null, lons)) > 180;
      var sumLon = 0;
      var sumLat = 0;
      for (var k = 0; k < n; k++) {
        sumLon += (wrap && lons[k] < 0) ? lons[k] + 360 : lons[k];
        sumLat += lats[k];
      }
      var meanLon = sumLon / n;
      if (meanLon > 180) meanLon -= 360;
      return { lat: sumLat / n, lon: meanLon };
    }
    return null;
  }

  // Choose at most `cap` points. At or under the cap everything is returned in
  // response order. Over it, 'nearest' keeps the points closest to the view
  // centre (ordered nearest first) and 'rank' keeps the highest rank (unranked
  // last); ties always fall back to response order, so the choice is stable.
  function selectForDisplay(points, cap, mode, center) {
    var total = points.length;
    if (total <= cap) return { shown: points.slice(), total: total, capped: false };
    var c = center || DEFAULT_CENTER;
    var rows = [];
    for (var i = 0; i < total; i++) {
      var p = points[i];
      var key;
      if (mode === 'nearest') {
        key = haversineKm(c.lat, c.lon, p.lat, p.lon);
      } else {
        key = finiteOrNull(p.rank);
        if (key === null) key = -Infinity;
      }
      rows.push({ p: p, i: i, key: key });
    }
    rows.sort(function (a, b) {
      if (a.key === b.key) return a.i - b.i;
      if (mode === 'nearest') return a.key < b.key ? -1 : 1;
      return a.key > b.key ? -1 : 1;
    });
    var shown = [];
    for (var j = 0; j < cap; j++) shown.push(rows[j].p);
    return { shown: shown, total: total, capped: true };
  }

  function capNote(shown, total) {
    if (shown < total) return 'showing ' + formatInt(shown) + ' of ' + formatInt(total);
    return formatInt(shown) + ' shown';
  }

  // ---------------------------------------------------------------------
  // Refresh rules
  // ---------------------------------------------------------------------

  function canRefresh(lastFetchMs, nowMs) {
    if (lastFetchMs === null || lastFetchMs === undefined) return true;
    return (nowMs - lastFetchMs) >= MIN_REFRESH_S * 1000;
  }

  // Due for an automatic refresh: switched on, idle, last answer was good (a
  // down or failed feed is never retried on its own), and its interval is up.
  function isDue(feed, st, nowMs) {
    if (!feed || !st || !st.on || st.busy || st.status !== 'ok') return false;
    if (!(feed.refreshS > 0)) return false;
    var interval = Math.max(feed.refreshS, MIN_REFRESH_S) * 1000;
    return (nowMs - st.lastFetchMs) >= interval;
  }

  // ---------------------------------------------------------------------
  // Request paths
  // ---------------------------------------------------------------------

  function query(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i++) {
      parts.push(encodeURIComponent(pairs[i][0]) + '=' + encodeURIComponent(pairs[i][1]));
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  // Path plus query for one feed. `center` is the view centre {lat, lon}; the
  // feeds that need parameters take them from it.
  function buildPath(key, center) {
    var feed = feedByKey(key);
    if (!feed) return '';
    var c = center || DEFAULT_CENTER;
    if (feed.params === 'camera') {
      return feed.endpoint + query([
        ['lat', c.lat.toFixed(3)], ['lon', c.lon.toFixed(3)],
        ['radius_km', '400'], ['limit', '100']
      ]);
    }
    if (feed.params === 'bbox') {
      var box = bboxAround(c.lat, c.lon, BBOX_SPAN_DEG);
      return feed.endpoint + query([
        ['south', box.south.toFixed(4)], ['west', box.west.toFixed(4)],
        ['north', box.north.toFixed(4)], ['east', box.east.toFixed(4)]
      ]);
    }
    if (feed.params === 'city') {
      return feed.endpoint + query([['city', nearestCity(c.lat, c.lon)]]);
    }
    return feed.endpoint;
  }

  // ---------------------------------------------------------------------
  // Row mappers: one proxy record in, one drawable point (or null) out
  // ---------------------------------------------------------------------
  //
  // A point is { name, lat, lon, rank, rows, color }. `rows` is the list of
  // [label, value] pairs the page's info panel prints; empty values are left
  // out. A record without a usable coordinate pair, or sitting exactly at
  // 0,0 (the placeholder feeds use for "unknown"), maps to null and is counted
  // as dropped.

  var SEVERITY_RANK = { Extreme: 4, Severe: 3, Moderate: 2, Minor: 1 };
  var NAV_STATUS = {
    0: 'under way using engine', 1: 'at anchor', 2: 'not under command',
    3: 'restricted manoeuvrability', 4: 'constrained by draught', 5: 'moored',
    6: 'aground', 7: 'engaged in fishing', 8: 'under way sailing', 15: 'not defined'
  };

  function addRow(rows, label, value, max) {
    var v = cleanText(value, max || 200);
    if (v !== '') rows.push([label, v]);
  }

  function makePoint(name, lat, lon, rank, rows, color) {
    var la = toCoord(lat, 90);
    var lo = toCoord(lon, 180);
    if (la === null || lo === null || (la === 0 && lo === 0)) return null;
    return { name: name, lat: la, lon: lo, rank: rank, rows: rows, color: color || null };
  }

  function countText(v) {
    var n = finiteOrNull(v);
    return n === null ? 'n/a' : String(n);
  }

  function eruptionYear(v) {
    if (typeof v === 'number' && isFinite(v)) return Math.round(v);
    if (typeof v === 'string' && /^-?\d+$/.test(v.trim())) return parseInt(v.trim(), 10);
    return null;
  }

  function clockFromHhmm(v) {
    var s = cleanText(v, 8);
    if (!/^\d{3,4}$/.test(s)) return '';
    while (s.length < 4) s = '0' + s;
    return s.slice(0, 2) + ':' + s.slice(2);
  }

  var MAPPERS = {
    launches: function (r) {
      var rows = [];
      addRow(rows, 'NET (UTC)', r.net);
      addRow(rows, 'STATUS', r.status);
      addRow(rows, 'PROVIDER', r.provider);
      addRow(rows, 'MISSION', r.mission);
      addRow(rows, 'ORBIT', r.orbit);
      addRow(rows, 'PAD', r.pad);
      addRow(rows, 'SITE', r.site);
      return makePoint(cleanText(r.name, 120) || 'Launch', r.lat, r.lon, null, rows);
    },

    volcanoes: function (r) {
      var year = eruptionYear(r.last_eruption);
      var rows = [];
      addRow(rows, 'COUNTRY', r.country);
      addRow(rows, 'VOLCANO TYPE', r.type);
      var elev = finiteOrNull(r.elevation_m);
      if (elev !== null && elev !== 0) rows.push(['ELEVATION', String(Math.round(elev)) + ' m']);
      rows.push(['LAST ERUPTION',
        year === null ? 'unknown' : (year < 0 ? String(-year) + ' BCE' : String(year))]);
      return makePoint(cleanText(r.name, 120) || 'Volcano', r.lat, r.lon, year, rows);
    },

    weatheralerts: function (r) {
      var c = ringCentroid(r.rings);
      if (!c) return null;
      var rows = [];
      addRow(rows, 'SEVERITY', r.severity);
      addRow(rows, 'URGENCY', r.urgency);
      addRow(rows, 'AREA', r.area);
      addRow(rows, 'EXPIRES', r.expires);
      addRow(rows, 'HEADLINE', r.headline, 240);
      var color = (typeof r.color === 'string' && /^#[0-9a-fA-F]{6}$/.test(r.color)) ? r.color : null;
      var rank = SEVERITY_RANK[r.severity] || 0;
      return makePoint(cleanText(r.event, 120) || 'Weather alert', c.lat, c.lon, rank, rows, color);
    },

    gdelt: function (r) {
      var count = finiteOrNull(typeof r.count === 'string' ? Number(r.count) : r.count);
      var rows = [];
      if (count !== null) rows.push(['ARTICLES', String(Math.round(count))]);
      return makePoint(cleanText(r.name, 120) || 'Unnamed place', r.lat, r.lon, count, rows);
    },

    radio: function (r) {
      var rows = [];
      addRow(rows, 'COUNTRY', r.country);
      var bitrate = finiteOrNull(r.bitrate);
      if (bitrate !== null && bitrate > 0) rows.push(['BITRATE', String(Math.round(bitrate)) + ' kbps']);
      addRow(rows, 'TAGS', r.tags, 120);
      // Shown as text only; the page never turns a feed value into a link.
      if (typeof r.url === 'string' && /^https?:\/\//i.test(r.url)) addRow(rows, 'STREAM', r.url, 200);
      return makePoint(cleanText(r.name, 120) || 'Radio station', r.lat, r.lon, null, rows);
    },

    bikeshare: function (r) {
      var rows = [];
      rows.push(['BIKES', countText(r.bikes_available)]);
      rows.push(['DOCKS', countText(r.docks_available)]);
      rows.push(['CAPACITY', countText(r.capacity)]);
      return makePoint(cleanText(r.name, 120) || 'Station', r.lat, r.lon, null, rows);
    },

    military: function (r) {
      var kind = cleanText(r.kind, 60);
      var name = cleanText(r.name, 120);
      if (name === '' || name.toUpperCase() === 'UNNAMED') name = 'Unnamed ' + (kind || 'site');
      var rows = [];
      addRow(rows, 'KIND', kind);
      addRow(rows, 'OPERATOR', r.operator);
      return makePoint(name, r.lat, r.lon, null, rows);
    },

    fires: function (r) {
      var date = cleanText(r.acq_date, 10);
      var time = clockFromHhmm(r.acq_time);
      var frp = finiteOrNull(r.frp_mw);
      var bright = finiteOrNull(r.brightness_k);
      var rows = [];
      if (frp !== null && frp > 0) rows.push(['FRP', round1(frp) + ' MW']);
      if (bright !== null && bright > 0) rows.push(['BRIGHTNESS', round1(bright) + ' K']);
      addRow(rows, 'CONFIDENCE', r.confidence);
      if (date) rows.push(['DETECTED', date + (time ? ' ' + time + ' UTC' : '')]);
      addRow(rows, 'SATELLITE', r.satellite);
      addRow(rows, 'DAY/NIGHT', r.daynight);
      var name = date ? 'Fire ' + date + (time ? ' ' + time + ' UTC' : '') : 'Fire detection';
      return makePoint(name, r.lat, r.lon, frp, rows);
    },

    ais: function (r) {
      var mmsi = cleanText(r.mmsi, 20);
      var rows = [];
      addRow(rows, 'MMSI', mmsi);
      var sog = finiteOrNull(r.sog_kn);
      if (sog !== null && sog >= 0 && sog < 102.3) rows.push(['SPEED', round1(sog) + ' kn']);
      var cog = finiteOrNull(r.cog_deg);
      if (cog !== null && cog >= 0 && cog < 360) rows.push(['COURSE', String(Math.round(cog)) + ' deg']);
      var hdg = finiteOrNull(r.heading_deg);
      if (hdg !== null && hdg >= 0 && hdg < 360) rows.push(['HEADING', String(Math.round(hdg)) + ' deg']);
      var nav = finiteOrNull(r.nav_status);
      if (nav !== null) {
        rows.push(['NAV STATUS', NAV_STATUS[nav] !== undefined ? NAV_STATUS[nav] : 'code ' + nav]);
      }
      addRow(rows, 'REPORTED', r.time_utc);
      var name = cleanText(r.name, 60) || (mmsi ? 'MMSI ' + mmsi : 'Vessel');
      return makePoint(name, r.lat, r.lon, null, rows);
    }
  };

  // ---------------------------------------------------------------------
  // Space weather (shown in the panel, not on the globe)
  // ---------------------------------------------------------------------

  function parseSpaceWeather(data) {
    var alerts = [];
    var src = Array.isArray(data.alerts) ? data.alerts : [];
    for (var i = 0; i < src.length && alerts.length < 8; i++) {
      var a = src[i];
      if (!a || typeof a !== 'object') continue;
      var headline = cleanText(a.headline, 160);
      if (headline === '') continue;
      alerts.push({
        id: cleanText(a.product_id, 20),
        issued: cleanText(a.issued, 40),
        headline: headline
      });
    }
    return {
      kp: finiteOrNull(data.kp),
      kpTime: cleanText(data.kp_time, 40),
      gScale: cleanText(data.g_scale, 4),
      windSpeed: finiteOrNull(data.solar_wind_speed_km_s),
      windDensity: finiteOrNull(data.solar_wind_density_p_cm3),
      windTime: cleanText(data.solar_wind_time, 40),
      alerts: alerts
    };
  }

  // ---------------------------------------------------------------------
  // One proxy answer to one status
  // ---------------------------------------------------------------------
  //
  // state is one of
  //   ok      the proxy answered; `points` (or `weather`) holds the records
  //   nokey   the proxy reports its API key is not configured
  //   down    the proxy answered but its upstream source is unavailable
  //   error   the request itself failed (HTTP error, bad JSON, wrong shape)

  function interpret(key, httpStatus, data) {
    var feed = feedByKey(key);
    var res = {
      key: key, state: 'error', message: '', keyEnv: '', note: '',
      points: [], dropped: 0, total: 0, weather: null
    };
    if (!feed) { res.message = 'unknown feed'; return res; }
    if (httpStatus !== 200) {
      res.message = 'request failed (HTTP ' + httpStatus + ')';
      return res;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      res.message = 'unexpected response';
      return res;
    }
    if (data.key_configured === false) {
      res.state = 'nokey';
      res.keyEnv = feed.keyEnv;
      res.message = 'Needs ' + (feed.keyEnv || 'an API key') + ' on the server. The key is free' +
        (feed.keyHint ? ': ' + feed.keyHint : '') + '.';
      return res;
    }
    var hasError = typeof data.error === 'string' && data.error !== '';

    if (feed.kind === 'panel') {
      res.weather = parseSpaceWeather(data);
      var w = res.weather;
      var empty = w.kp === null && w.windSpeed === null && w.alerts.length === 0;
      if (hasError && empty) {
        res.state = 'down';
        res.message = cleanText(data.error, 120);
        return res;
      }
      res.state = 'ok';
      return res;
    }

    var list = data[feed.listKey];
    if (!Array.isArray(list)) {
      res.message = 'unexpected response';
      return res;
    }
    if (hasError && list.length === 0) {
      res.state = 'down';
      res.message = cleanText(data.error, 120);
      return res;
    }
    var mapper = MAPPERS[key];
    for (var i = 0; i < list.length; i++) {
      var row = list[i];
      var point = (row && typeof row === 'object') ? mapper(row) : null;
      if (point) res.points.push(point); else res.dropped++;
    }
    res.state = 'ok';
    res.total = res.points.length;
    if (hasError) res.note = cleanText(data.error, 120);
    if (key === 'ais' && list.length === 0 && data.connected === false && !hasError) {
      res.note = 'connecting to AISStream; vessels appear over the next few minutes';
    }
    return res;
  }

  // ---------------------------------------------------------------------
  // Panel and globe
  // ---------------------------------------------------------------------

  var STYLE_TEXT = [
    '.sdalayers-status { font-size: 9px; line-height: 1.4; padding: 0 0 4px 20px;',
    '  color: var(--hud-dim, #7a5500); word-break: break-word; }',
    '.sdalayers-refresh { margin: 0 0 6px 20px; }',
    '.sdalayers-refresh:disabled { opacity: 0.4; cursor: default; }',
    '.sdalayers-detail { font-size: 10px; line-height: 1.5; padding: 2px 0 6px 20px; }',
    '.sdalayers-line { display: flex; gap: 8px; }',
    '.sdalayers-key { color: var(--hud-dim, #7a5500); min-width: 64px; }',
    '.sdalayers-note { font-size: 10px; line-height: 1.5; opacity: 0.85; }'
  ].join('\n');

  function nowMs() { return Date.now(); }

  function el(doc, tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function injectStyle(doc) {
    var existing = typeof doc.getElementById === 'function' ? doc.getElementById('sdalayers-style') : null;
    if (existing) return;
    var style = doc.createElement('style');
    style.id = 'sdalayers-style';
    style.textContent = STYLE_TEXT;
    (doc.head || doc.documentElement).appendChild(style);
  }

  // Where the globe is looking, as {lat, lon} degrees. Falls back to the camera
  // position, then to the page's opening view, so a feed never lacks a centre.
  function cameraCenter(viewer) {
    var Cesium = window.Cesium;
    try {
      var canvas = viewer.scene.canvas;
      var mid = new Cesium.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2);
      var hit = viewer.camera.pickEllipsoid(mid, viewer.scene.globe.ellipsoid);
      var carto = hit ? Cesium.Cartographic.fromCartesian(hit) : viewer.camera.positionCartographic;
      if (carto) {
        var lat = carto.latitude / DEG;
        var lon = carto.longitude / DEG;
        if (isFinite(lat) && isFinite(lon)) return { lat: lat, lon: lon };
      }
    } catch (e) { /* fall through to the default */ }
    return { lat: DEFAULT_CENTER.lat, lon: DEFAULT_CENTER.lon };
  }

  function init(ctx) {
    var mount = ctx && ctx.mount;
    var doc = mount && (mount.ownerDocument || window.document);
    if (!mount || !doc) return null;
    var Cesium = window.Cesium;

    injectStyle(doc);
    mount.textContent = '';
    var panel = el(doc, 'div', 'panel sdalayers-panel');
    var heading = el(doc, 'h3', '', 'World Feeds');
    var body = el(doc, 'div', 'panel-body');
    panel.appendChild(heading);
    panel.appendChild(body);
    mount.appendChild(panel);

    // The page collapses every ".panel h3" it finds at load. Handling the click
    // here and stopping it keeps this panel to one toggle whatever the init order.
    heading.addEventListener('click', function (evt) {
      if (evt && evt.stopImmediatePropagation) evt.stopImmediatePropagation();
      panel.classList.toggle('collapsed');
    });

    if (ctx.isStatic) {
      body.appendChild(el(doc, 'div', 'sdalayers-note',
        'Data feeds need the live server, which this static demo does not have.'));
      return { isStatic: true, setEnabled: function () {}, destroy: function () {} };
    }
    if (!Cesium || !ctx.viewer) {
      body.appendChild(el(doc, 'div', 'sdalayers-note',
        'Data feeds need the Cesium globe, which did not load.'));
      return { isStatic: false, setEnabled: function () {}, destroy: function () {} };
    }

    var viewer = ctx.viewer;
    var apiBase = ctx.api || '';
    var states = [];
    var byKey = {};
    var lastIntel = {};

    function intel(key, text) {
      if (lastIntel[key] === text) return;
      lastIntel[key] = text;
      try { if (ctx.addIntelItem) ctx.addIntelItem(text); } catch (e) { /* the feed log is optional */ }
    }

    function setStatus(st, text) {
      st.els.status.textContent = text;
      st.els.status.style.display = text ? '' : 'none';
    }

    function clearEntities(st) {
      if (!st.ds) return;
      st.ds.entities.removeAll();
      st.ds.show = false;
    }

    function requestRender() {
      try {
        if (viewer.scene && typeof viewer.scene.requestRender === 'function') viewer.scene.requestRender();
      } catch (e) { /* rendering continues on the next frame */ }
    }

    function ensureDataSource(st) {
      if (!st.ds) {
        st.ds = new Cesium.CustomDataSource('sdalayers-' + st.feed.key);
        var added = viewer.dataSources.add(st.ds);
        if (added && typeof added.then === 'function') added.then(null, function () {});
      }
      return st.ds;
    }

    function draw(st, points) {
      var ds = ensureDataSource(st);
      var entities = ds.entities;
      if (entities.suspendEvents) entities.suspendEvents();
      try {
        entities.removeAll();
        for (var i = 0; i < points.length; i++) {
          var p = points[i];
          var color = Cesium.Color.fromCssColorString(p.color || st.feed.color);
          entities.add({
            id: 'sdalayers:' + st.feed.key + ':' + i,
            name: p.name,
            position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 0),
            point: {
              pixelSize: st.feed.size,
              color: color.withAlpha(0.8),
              outlineColor: color,
              outlineWidth: 1
            },
            properties: { type: st.feed.type, feed: st.feed.key, detail_rows: p.rows }
          });
        }
      } finally {
        if (entities.resumeEvents) entities.resumeEvents();
      }
      ds.show = true;
      requestRender();
    }

    function addLine(box, label, value) {
      var line = el(doc, 'div', 'sdalayers-line');
      line.appendChild(el(doc, 'span', 'sdalayers-key', label));
      line.appendChild(el(doc, 'span', 'sdalayers-value', value));
      box.appendChild(line);
    }

    function renderWeather(st, w) {
      var box = st.els.detail;
      box.textContent = '';
      box.style.display = '';
      addLine(box, 'Kp', w.kp === null ? 'n/a' : w.kp.toFixed(2) + (w.gScale ? ' (' + w.gScale + ')' : ''));
      var wind = 'n/a';
      if (w.windSpeed !== null) {
        wind = String(Math.round(w.windSpeed)) + ' km/s';
        if (w.windDensity !== null) wind += ', ' + round1(w.windDensity) + ' p/cm3';
      }
      addLine(box, 'Solar wind', wind);
      if (w.alerts.length === 0) {
        addLine(box, 'Alerts', 'none');
      }
      for (var i = 0; i < w.alerts.length; i++) {
        addLine(box, i === 0 ? 'Alerts' : '', (w.alerts[i].issued ? w.alerts[i].issued.slice(0, 16) + ' ' : '') +
          w.alerts[i].headline);
      }
    }

    function nextLaunch(points, now) {
      var best = null;
      for (var i = 0; i < points.length; i++) {
        var rows = points[i].rows;
        for (var j = 0; j < rows.length; j++) {
          if (rows[j][0] !== 'NET (UTC)') continue;
          var t = Date.parse(rows[j][1]);
          if (isFinite(t) && t > now && (best === null || t < best)) best = t;
        }
      }
      return best === null ? '' : formatCountdown(new Date(best).toISOString(), now);
    }

    function okStatus(st, res, sel) {
      var feed = st.feed;
      var parts = [capNote(sel.shown.length, sel.total) + ', updated ' + formatClock(new Date(st.answeredMs))];
      if (res.dropped > 0) parts.push(formatInt(res.dropped) + ' skipped (no usable coordinates)');
      if (feed.key === 'launches') {
        var next = nextLaunch(res.points, st.answeredMs);
        if (next) parts.push('next ' + next);
      }
      if (feed.params === 'city' && st.cityKey) parts.push(CITIES[st.cityKey].label);
      if (feed.params === 'camera' || feed.params === 'bbox') parts.push('around the view centre');
      if (res.note) parts.push(res.note);
      return parts.join(', ');
    }

    function showRefresh(st, visible) {
      st.els.refresh.style.display = visible ? '' : 'none';
      st.els.refresh.disabled = true;
    }

    function applyResult(st, res) {
      var feed = st.feed;
      var stamp = formatClock(new Date(st.answeredMs));
      try {
        if (res.state === 'nokey') {
          st.on = false;
          st.els.box.checked = false;
          st.els.box.disabled = true;
          clearEntities(st);
          st.els.count.textContent = '';
          st.els.detail.style.display = 'none';
          showRefresh(st, false);
          setStatus(st, res.message);
          intel(feed.key, feed.label + ': needs ' + res.keyEnv + ' (free key)');
          return;
        }
        if (res.state === 'down' || res.state === 'error') {
          clearEntities(st);
          st.els.count.textContent = '';
          st.els.detail.style.display = 'none';
          showRefresh(st, true);
          setStatus(st, res.state === 'down'
            ? 'source unavailable at ' + stamp + (res.message ? ' (' + res.message + ')' : '')
            : res.message + ' at ' + stamp);
          intel(feed.key, feed.label + ': ' + (res.state === 'down' ? 'source unavailable' : res.message));
          return;
        }
        // ok
        showRefresh(st, true);
        if (feed.kind === 'panel') {
          clearEntities(st);
          renderWeather(st, res.weather);
          st.els.count.textContent = '';
          setStatus(st, 'updated ' + stamp);
          intel(feed.key, feed.label + ': updated');
          return;
        }
        var sel = selectForDisplay(res.points, feed.cap, feed.select, st.center);
        draw(st, sel.shown);
        st.els.count.textContent = formatInt(sel.shown.length);
        setStatus(st, okStatus(st, res, sel));
        intel(feed.key, feed.label + ': ' + capNote(sel.shown.length, sel.total) + ' on the globe');
      } catch (e) {
        clearEntities(st);
        st.els.count.textContent = '';
        setStatus(st, 'could not draw this feed at ' + stamp);
      }
    }

    function fetchFeed(st) {
      st.busy = true;
      st.lastFetchMs = nowMs();
      st.center = cameraCenter(viewer);
      st.cityKey = st.feed.params === 'city' ? nearestCity(st.center.lat, st.center.lon) : '';
      setStatus(st, 'loading...');
      var url = apiBase + buildPath(st.feed.key, st.center);
      var request;
      try { request = window.fetch(url); } catch (e) { request = Promise.reject(e); }
      request.then(function (resp) {
        var code = resp.status;
        return resp.json().then(
          function (data) { return { code: code, data: data }; },
          function () { return { code: code, data: null }; });
      }).then(null, function () {
        return { code: 0, data: null, failed: true };
      }).then(function (r) {
        st.busy = false;
        st.answeredMs = nowMs();
        st.result = r.failed
          ? { key: st.feed.key, state: 'error', message: 'network error', keyEnv: '', note: '',
              points: [], dropped: 0, total: 0, weather: null }
          : interpret(st.feed.key, r.code, r.data);
        st.status = st.result.state;
        if (st.on) applyResult(st, st.result);
      }).then(null, function () { st.busy = false; });
    }

    function enable(st) {
      st.on = true;
      st.els.box.checked = true;
      if (st.busy) { setStatus(st, 'loading...'); return; }
      if (st.result && !canRefresh(st.lastFetchMs, nowMs())) {
        // Inside the five-minute window: show the last answer again, no request.
        applyResult(st, st.result);
        return;
      }
      fetchFeed(st);
    }

    function disable(st) {
      st.on = false;
      st.els.box.checked = false;
      clearEntities(st);
      st.els.count.textContent = '';
      st.els.detail.style.display = 'none';
      showRefresh(st, false);
      setStatus(st, '');
      requestRender();
    }

    function setEnabled(key, on) {
      var st = byKey[key];
      if (!st || st.els.box.disabled) return;
      if (on) enable(st); else disable(st);
    }

    function refreshNow(st) {
      if (!st.on || st.busy || !canRefresh(st.lastFetchMs, nowMs())) return;
      fetchFeed(st);
    }

    function tick() {
      var now = nowMs();
      for (var i = 0; i < states.length; i++) {
        var st = states[i];
        try {
          if (isDue(st.feed, st, now)) fetchFeed(st);
          if (st.on) st.els.refresh.disabled = st.busy || !canRefresh(st.lastFetchMs, now);
        } catch (e) { /* one feed's failure must not stop the others */ }
      }
    }

    function buildRow(feed) {
      var st = {
        feed: feed, on: false, busy: false, status: 'idle', lastFetchMs: null,
        answeredMs: null, result: null, center: null, cityKey: '', ds: null, els: {}
      };
      var row = el(doc, 'div', 'sdalayers-feed');
      var label = el(doc, 'label', 'layer-toggle');
      label.title = attributionLine(feed.key);
      var box = doc.createElement('input');
      box.type = 'checkbox';
      box.id = 'sdalayers-' + feed.key;
      var name = el(doc, 'span', '', feed.label);
      var count = el(doc, 'span', 'count', '');
      count.id = 'sdalayers-count-' + feed.key;
      label.appendChild(box);
      label.appendChild(name);
      label.appendChild(count);

      var status = el(doc, 'div', 'sdalayers-status', '');
      status.id = 'sdalayers-status-' + feed.key;
      status.style.display = 'none';

      var detail = el(doc, 'div', 'sdalayers-detail', '');
      detail.id = 'sdalayers-detail-' + feed.key;
      detail.style.display = 'none';

      var refresh = el(doc, 'button', 'preset-btn sdalayers-refresh', 'Refresh');
      refresh.id = 'sdalayers-refresh-' + feed.key;
      refresh.type = 'button';
      refresh.title = 'A feed can be refreshed once every ' + (MIN_REFRESH_S / 60) + ' minutes';
      refresh.style.display = 'none';
      refresh.disabled = true;

      row.appendChild(label);
      row.appendChild(status);
      if (feed.kind === 'panel') row.appendChild(detail);
      row.appendChild(refresh);
      body.appendChild(row);

      st.els = { box: box, count: count, status: status, detail: detail, refresh: refresh };
      box.addEventListener('change', function () {
        if (box.checked) enable(st); else disable(st);
      });
      refresh.addEventListener('click', function () { refreshNow(st); });
      states.push(st);
      byKey[feed.key] = st;
    }

    for (var i = 0; i < FEEDS.length; i++) buildRow(FEEDS[i]);

    var timer = window.setInterval(tick, TICK_MS);
    return {
      isStatic: false,
      setEnabled: setEnabled,
      destroy: function () {
        window.clearInterval(timer);
        for (var j = 0; j < states.length; j++) clearEntities(states[j]);
      }
    };
  }

  window.SDALayers = {
    init: init,
    FEEDS: FEEDS,
    CITIES: CITIES,
    MIN_REFRESH_S: MIN_REFRESH_S,
    DEFAULT_CAP: DEFAULT_CAP,
    feedByKey: feedByKey,
    attributionLine: attributionLine,
    toCoord: toCoord,
    cleanText: cleanText,
    formatInt: formatInt,
    formatClock: formatClock,
    formatCountdown: formatCountdown,
    haversineKm: haversineKm,
    bboxAround: bboxAround,
    nearestCity: nearestCity,
    ringCentroid: ringCentroid,
    selectForDisplay: selectForDisplay,
    capNote: capNote,
    canRefresh: canRefresh,
    isDue: isDue,
    buildPath: buildPath,
    interpret: interpret,
    parseSpaceWeather: parseSpaceWeather
  };
})(window);
