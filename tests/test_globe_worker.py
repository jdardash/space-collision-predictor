"""The globe's SGP4 worker must keep its output aligned with the page's satellite list.

The page (worldview.html, applyWorkerPositions) maps slot i of the worker's positions
buffer to satEngine.sats[i] by array index, and on the worker path it does not validate
TLEs itself. So the worker owes the page one slot per catalog record, in catalog order,
whether or not that record builds or propagates. A record that has no solution keeps its
slot and carries a NaN longitude, the one value the page treats as "no position" (it hides
the point). Dropping the record instead shifts every later dot onto its neighbour's
position, which puts the wrong name and NORAD id under a clicked dot.

These tests run the real worker file in Node against a stub satellite.js. The stub encodes
the NORAD id into every number it returns, so a slot that holds the wrong object is
visible at a glance. They skip cleanly when Node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKER = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "js" / "sgp4-worker.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# Runs the worker in a vm context that fakes the worker globals. argv[2] is the worker
# path; stdin is {"steps": [...]}. Each step is a message for self.onmessage; a positions
# step may set buffer to "recycle" to hand back the buffer from the previous positions
# reply, which is what the page does every frame. Record line1 is "<id>|<mode>|<arg>".
HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
const outbox = [];

function parse(line) {
  const p = String(line).split('|');
  return { id: Number(p[0]), mode: p[1] || 'ok', arg: Number(p[2] || 0) };
}

const BUILD_MODES = ['build_error', 'build_throw', 'build_null'];

const satellite = {
  twoline2satrec(line1) {
    const r = parse(line1);
    if (r.mode === 'build_throw') throw new Error('bad tle');
    if (r.mode === 'build_null') return null;
    if (r.mode === 'build_error') return { error: 1, no: 0.06, tag: r };
    return { error: 0, no: 0.06, tag: r };
  },
  propagate(satrec, when) {
    const r = satrec.tag;
    const failing = BUILD_MODES.indexOf(r.mode) < 0 && r.mode !== 'ok' &&
      when.getTime() >= r.arg;
    if (failing && r.mode === 'prop_throw') throw new Error('sgp4 failed');
    if (failing && r.mode === 'no_solution') return { position: false, velocity: false };
    if (failing && r.mode === 'nan') {
      const nan = { x: NaN, y: NaN, z: NaN };
      return { position: nan, velocity: nan };
    }
    if (failing && r.mode === 'nan_height') {
      return { position: { x: r.id, y: r.id, z: NaN }, velocity: { x: r.id, y: 0, z: 0 } };
    }
    return { position: { x: r.id, y: r.id, z: r.id }, velocity: { x: r.id, y: 0, z: 0 } };
  },
  gstime() { return 0; },
  eciToGeodetic(pos) {
    return { longitude: pos.x * 0.25, latitude: pos.y * 0.125, height: pos.z + 400 };
  },
};

const sandbox = {
  self: {},
  postMessage(msg) { outbox.push(msg); },
  importScripts() { sandbox.satellite = satellite; },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox, { filename: 'worker' });

function summarise(msg) {
  if (msg.type === 'positions') {
    const view = new Float32Array(msg.buffer);
    const slots = [];
    for (let i = 0; i < msg.count; i++) {
      slots.push(Array.from(view.subarray(i * 4, i * 4 + 4)));
    }
    return { type: msg.type, t: msg.t, count: msg.count, slots: slots };
  }
  if (msg.type === 'track') {
    return { type: msg.type, id: msg.id, count: msg.count,
             orbit: Array.from(msg.orbit), ground: Array.from(msg.ground) };
  }
  return msg;
}

const results = [];
let lastBuffer = null;
for (const step of scenario.steps) {
  const msg = Object.assign({}, step);
  if (msg.buffer === 'recycle') msg.buffer = lastBuffer;
  outbox.length = 0;
  sandbox.self.onmessage({ data: msg });
  for (const out of outbox) {
    if (out.type === 'positions') lastBuffer = out.buffer;
  }
  results.push(outbox.map(summarise));
}
process.stdout.write(JSON.stringify(results, (k, v) =>
  (typeof v === 'number' && Number.isNaN(v)) ? 'NaN' : v));
"""

IDS = [11, 22, 33, 44, 55]


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("globe_worker") / "harness.js"
    path.write_text(HARNESS, encoding="ascii")
    return path


def run_worker(harness: Path, steps: list[dict]) -> list[list[dict]]:
    proc = subprocess.run(
        ["node", str(harness), str(WORKER)],
        input=json.dumps({"steps": steps}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def catalog(modes: dict[int, str] | None = None, ids: list[int] | None = None) -> dict:
    """A catalog message; modes maps slot index to a stub failure mode (default 'ok')."""
    modes = modes or {}
    sats = []
    for slot, norad_id in enumerate(ids or IDS):
        mode = modes.get(slot, "ok")
        line1 = f"{norad_id}|{mode}|2000"
        sats.append({"norad_id": norad_id, "line1": line1, "line2": "unused"})
    return {"type": "catalog", "sats": sats}


def good_slot(norad_id: int) -> list[float]:
    """What the stub returns for an object: lon, lat, altitude, speed, all exact in float32."""
    return [norad_id * 0.25, norad_id * 0.125, norad_id + 400.0, float(norad_id)]


def assert_aligned(slots: list[list], bad: set[int], ids: list[int] | None = None) -> None:
    ids = ids or IDS
    assert len(slots) == len(ids), "one slot per catalog record, bad or not"
    for slot, norad_id in enumerate(ids):
        if slot in bad:
            assert slots[slot][0] == "NaN", f"slot {slot} must read as no position"
        else:
            assert slots[slot] == pytest.approx(good_slot(norad_id)), (
                f"slot {slot} must hold object {norad_id}, not a neighbour"
            )


def test_worker_file_passes_node_check():
    proc = subprocess.run(
        ["node", "--check", str(WORKER)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("mode", ["build_error", "build_throw", "build_null"])
def test_bad_record_in_the_middle_keeps_the_good_ones_in_their_slots(harness, mode):
    ready, positions = run_worker(
        harness,
        [catalog({2: mode}), {"type": "positions", "t": 1000, "buffer": None}],
    )

    assert ready == [{"type": "ready", "count": 4, "failed": 1}]
    (frame,) = positions
    assert frame["type"] == "positions"
    assert frame["count"] == 5
    assert_aligned(frame["slots"], bad={2})


@pytest.mark.parametrize("bad", [{0}, {4}, {1, 3}, {0, 1, 2, 3, 4}])
def test_alignment_holds_wherever_the_bad_records_sit(harness, bad):
    _, positions = run_worker(
        harness,
        [
            catalog({slot: "build_error" for slot in bad}),
            {"type": "positions", "t": 1000, "buffer": None},
        ],
    )

    (frame,) = positions
    assert_aligned(frame["slots"], bad=bad)


@pytest.mark.parametrize("mode", ["no_solution", "prop_throw", "nan", "nan_height"])
def test_propagation_failing_at_one_tick_hides_only_that_slot(harness, mode):
    # The record builds fine and propagates at t=1000, then fails from t=2000 on, as a
    # decayed object does. The second request recycles the first reply's buffer, so a
    # slot that is not rewritten would still show the stale (valid) position.
    first, second = run_worker(
        harness,
        [
            catalog({2: mode}),
            {"type": "positions", "t": 1000, "buffer": None},
            {"type": "positions", "t": 3000, "buffer": "recycle"},
        ],
    )[1:]

    assert_aligned(first[0]["slots"], bad=set())
    assert_aligned(second[0]["slots"], bad={2})


def test_a_failed_build_slot_is_rewritten_on_a_recycled_buffer(harness):
    _, first, second = run_worker(
        harness,
        [
            catalog({1: "build_error"}),
            {"type": "positions", "t": 1000, "buffer": None},
            {"type": "positions", "t": 2000, "buffer": "recycle"},
        ],
    )

    assert_aligned(first[0]["slots"], bad={1})
    assert_aligned(second[0]["slots"], bad={1})


def test_track_lookup_follows_the_object_not_the_slot(harness):
    # Object 44 sits in slot 3 but is the third usable record once 33 is dropped.
    # Its orbit must come from 44's own elements, and the dropped id must be refused.
    _, _, own, dropped = run_worker(
        harness,
        [
            catalog({2: "build_error"}),
            {"type": "positions", "t": 1000, "buffer": None},
            {"type": "track", "id": 44, "t": 1000, "points": 16, "revs": 1},
            {"type": "track", "id": 33, "t": 1000, "points": 16, "revs": 1},
        ],
    )

    (track,) = own
    assert track["type"] == "track" and track["id"] == 44
    assert track["orbit"][0:3] == pytest.approx(good_slot(44)[0:3])
    (err,) = dropped
    assert err["type"] == "error" and "33" in err["message"]


def test_a_new_catalog_replaces_the_old_slots(harness):
    short_ids = [66, 77, 88]
    _, _, ready, positions = run_worker(
        harness,
        [
            catalog({1: "build_error"}),
            {"type": "positions", "t": 1000, "buffer": None},
            catalog({0: "build_error"}, ids=short_ids),
            {"type": "positions", "t": 1000, "buffer": "recycle"},
        ],
    )

    assert ready == [{"type": "ready", "count": 2, "failed": 1}]
    (frame,) = positions
    assert_aligned(frame["slots"], bad={0}, ids=short_ids)
