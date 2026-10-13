// SGP4 propagation worker for the WorldView globe.
//
// Design adapted from satvis (https://github.com/Flowm/satvis, MIT license),
// src/modules/util/sgp4Worker.ts. Three properties are borrowed from it:
// the only state the worker keeps is a satrec cache, every request is a pure
// function of the timestamp it carries (so a dropped or reordered reply is
// harmless), and results come back as one packed typed array the main thread
// copies straight into its primitives. Buffers are passed as transferables and
// handed back on the next request, so a full-catalog frame allocates nothing.
//
// Protocol, main thread to worker:
//   { type: 'catalog',   sats: [{ norad_id, line1, line2 }, ...] }
//   { type: 'positions', t: <epoch ms>, buffer: <ArrayBuffer or null> }
//   { type: 'track',     id: <norad id>, t: <epoch ms>, points: <int>, revs: <float> }
//
// Protocol, worker to main thread:
//   { type: 'ready',     count, failed }     // count = records that built, failed = the rest
//   { type: 'positions', t, count, buffer }  // Float32Array, STRIDE floats per object
//   { type: 'track',     id, orbit, ground } // Float32Array(m*3), Float32Array(m*2)
//   { type: 'error',     message }
//
// Alignment: slot i of every positions buffer belongs to sats[i] of the catalog
// message, and count is the catalog length, not the number of records that built.
// The page matches slots to its own satellite list by array index and does not
// validate TLEs on the worker path, so a record that fails to build or to
// propagate keeps its slot and reads as "no position" (NaN longitude). Dropping it
// would shift every later dot onto its neighbour.
//
// Frame is TEME (ECI) out of SGP4, converted to geodetic with the GMST of the
// requested instant, which is exactly what the server does in
// routes/worldview.py. Gravity model is WGS72, the satellite.js default and the
// one the Python side uses.

/* global importScripts, satellite, postMessage, self */

var SGP4_CDN = 'https://cdn.jsdelivr.net/npm/satellite.js@5.0.0/dist/satellite.min.js';

// Floats written per object into the positions buffer:
//   0 longitude (rad), 1 latitude (rad), 2 altitude (km), 3 speed (km/s)
// A NaN longitude means no position for that object this tick: its TLE did not
// build, or SGP4 produced no solution (decayed, bad elements).
var STRIDE = 4;

var libraryReady = false;
var records = [];   // one per catalog record, same order: { id: number, satrec: object or null }
var indexById = {}; // norad id -> slot of a record that built

try {
  importScripts(SGP4_CDN);
  libraryReady = typeof satellite !== 'undefined';
} catch (e) {
  libraryReady = false;
}

if (!libraryReady) {
  postMessage({ type: 'error', message: 'satellite.js could not be loaded in the worker' });
}

function buildCatalog(list) {
  records = [];
  indexById = {};
  var failed = 0;
  for (var i = 0; i < list.length; i++) {
    var rec = list[i] || {};
    var satrec = null;
    try {
      satrec = satellite.twoline2satrec(rec.line1, rec.line2);
    } catch (e) {
      satrec = null;
    }
    if (!satrec || satrec.error) {
      // Keep the slot so every later record stays aligned with the page's list.
      failed++;
      records.push({ id: rec.norad_id, satrec: null });
      continue;
    }
    indexById[rec.norad_id] = records.length;
    records.push({ id: rec.norad_id, satrec: satrec });
  }
  postMessage({ type: 'ready', count: records.length - failed, failed: failed });
}

// Every slot is rewritten each tick, so a recycled buffer never carries a stale position.
function markNoPosition(out, base) {
  out[base] = NaN;
  out[base + 1] = 0;
  out[base + 2] = 0;
  out[base + 3] = 0;
}

function propagateAll(tMs, buffer) {
  var n = records.length;
  var needed = n * STRIDE * Float32Array.BYTES_PER_ELEMENT;
  if (!buffer || buffer.byteLength < needed) {
    buffer = new ArrayBuffer(needed);
  }
  var out = new Float32Array(buffer, 0, n * STRIDE);
  var when = new Date(tMs);
  var gmst = satellite.gstime(when);

  for (var i = 0; i < n; i++) {
    var base = i * STRIDE;
    var satrec = records[i].satrec;
    if (!satrec) { markNoPosition(out, base); continue; }
    var pv = null;
    try {
      pv = satellite.propagate(satrec, when);
    } catch (e) {
      pv = null;
    }
    if (!pv || !pv.position) { markNoPosition(out, base); continue; }
    var gd = satellite.eciToGeodetic(pv.position, gmst);
    // The page tests only the longitude, so never send a slot that is half finite.
    if (!isFinite(gd.longitude) || !isFinite(gd.latitude) || !isFinite(gd.height)) {
      markNoPosition(out, base);
      continue;
    }
    out[base] = gd.longitude;
    out[base + 1] = gd.latitude;
    out[base + 2] = gd.height;
    if (pv.velocity) {
      out[base + 3] = Math.sqrt(
        pv.velocity.x * pv.velocity.x +
        pv.velocity.y * pv.velocity.y +
        pv.velocity.z * pv.velocity.z
      );
    } else {
      out[base + 3] = 0;
    }
  }

  postMessage({ type: 'positions', t: tMs, count: n, buffer: buffer }, [buffer]);
}

// Orbit ring and ground track for one object. Both are sampled over the same
// span so the ring and the trace on the surface line up point for point.
function propagateTrack(id, tMs, points, revs) {
  var idx = indexById[id];
  if (idx === undefined) {
    postMessage({ type: 'error', message: 'unknown norad id ' + id });
    return;
  }
  var satrec = records[idx].satrec;
  var periodMin = (2 * Math.PI) / satrec.no; // satrec.no is rad/min
  if (!isFinite(periodMin) || periodMin <= 0) {
    postMessage({ type: 'error', message: 'no usable period for ' + id });
    return;
  }
  periodMin = Math.min(periodMin, 1500);
  var nPts = Math.max(16, Math.min(1024, points || 128));
  var spanMs = periodMin * 60000 * (revs && revs > 0 ? revs : 1);
  var stepMs = spanMs / nPts;

  var orbit = new Float32Array((nPts + 1) * 3);
  var ground = new Float32Array((nPts + 1) * 2);
  var kept = 0;

  for (var i = 0; i <= nPts; i++) {
    var when = new Date(tMs + i * stepMs);
    var pv = null;
    try {
      pv = satellite.propagate(satrec, when);
    } catch (e) {
      pv = null;
    }
    if (!pv || !pv.position) continue;
    var gd = satellite.eciToGeodetic(pv.position, satellite.gstime(when));
    orbit[kept * 3] = gd.longitude;
    orbit[kept * 3 + 1] = gd.latitude;
    orbit[kept * 3 + 2] = gd.height;
    ground[kept * 2] = gd.longitude;
    ground[kept * 2 + 1] = gd.latitude;
    kept++;
  }

  var orbitOut = orbit.slice(0, kept * 3);
  var groundOut = ground.slice(0, kept * 2);
  postMessage(
    { type: 'track', id: id, count: kept, orbit: orbitOut, ground: groundOut },
    [orbitOut.buffer, groundOut.buffer]
  );
}

self.onmessage = function (event) {
  var msg = event.data || {};
  if (!libraryReady) {
    postMessage({ type: 'error', message: 'worker has no SGP4 library' });
    return;
  }
  try {
    if (msg.type === 'catalog') {
      buildCatalog(msg.sats || []);
    } else if (msg.type === 'positions') {
      propagateAll(msg.t, msg.buffer || null);
    } else if (msg.type === 'track') {
      propagateTrack(msg.id, msg.t, msg.points, msg.revs);
    }
  } catch (e) {
    postMessage({ type: 'error', message: String(e && e.message ? e.message : e) });
  }
};
