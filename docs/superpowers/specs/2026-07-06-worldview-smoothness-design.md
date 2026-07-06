# WorldView Smoothness and Visual Polish — Design

Date: 2026-07-06
Status: Approved (user: "do all" — Options A + B + C combined)

## Problem

The WorldView globe runs at 1-2 FPS and looks cluttered:

1. ~1,400 satellites are rendered as Cesium `Entity` objects, each with an
   always-visible text label (`worldview.html` `updateSatEntities`). Entity
   labels are the most expensive Cesium primitive; at this count the GPU
   spends the whole frame rebuilding label geometry.
2. Positions are hard-assigned every 10 s (`REFRESH_SATS_MS`); satellites
   freeze, then teleport. Time-warp playback fetches server positions every
   500 ms, so scrubbing is jerky too.
3. Every dot has identical brightness/size and a fuzzy translucent outline;
   no visual hierarchy. Collapsed left-panel sections look like dead chrome.

## Design

### Server (`src/sda/routes/worldview.py`)

New endpoint `GET /api/tles` returning, per tracked satellite:
`norad_id`, `name`, `line1`, `line2`, `regime`, `regime_color`.
Regime is classified from TLE mean elements (semi-major axis altitude via
WGS72 mu, eccentricity) using the existing `_classify_regime` — no full
propagation needed. Existing endpoints are unchanged (the legacy entity
path remains as a fallback).

### Client renderer (`src/sda/templates/worldview.html`)

- Load `satellite.js` (WGS72 SGP4, matching the project rule) from CDN.
- Fetch `/api/tles` once at startup (re-fetch every 30 min); build satrecs
  client-side.
- Render satellites as a `PointPrimitiveCollection` (one point per
  satellite) instead of entities. Labels live in a separate small
  `LabelCollection`.
- A `scene.preUpdate` frame loop propagates satellites and updates point
  positions. Updates are staggered round-robin (~1/4 of the catalog per
  frame) so per-frame cost stays under ~8 ms; each satellite refreshes at
  ~15 Hz, visually indistinguishable from continuous motion. GMST is
  computed once per frame batch.
- Simulation time = wall clock when LIVE; `timeState.epoch` when in
  history/playback mode. Time-warp playback therefore becomes fully
  client-side and smooth at any speed — no server fetches while scrubbing.
- `requestRenderMode` switches to continuous rendering (needed for motion).
- Fallback: if satellite.js or `/api/tles` fails, the legacy
  entity-per-satellite path still works (with labels off by default).

### Labels (declutter)

- Default: no satellite labels at global zoom.
- Always labeled: hovered satellite and selected satellite.
- When the camera is below ~8,000 km altitude, label up to 60 on-screen
  satellites (nearest first), refreshed on a 400 ms cadence.
- Detection mode keeps its identity but bounded: `sparse` caps at 40
  labels, `full` at 150 — no more 1,400-label soup.

### Selection emphasis

- Selected satellite: larger point, full-alpha outline, label with
  background, and a one-period orbit polyline computed client-side from
  its satrec (no server call). Camera flies to frame it once (no
  continuous tracking).
- Hover: pointer cursor + temporary label.
- Info panel values (altitude, velocity, regime) come from the live
  client-side propagation.

### Visual polish

- Collapsible panel headers get a chevron affordance and hover state so
  collapsed sections read as expandable, not dead.
- 150-200 ms ease transitions on panels, buttons, layer toggles, and the
  conjunction drawer.
- Point styling: size by regime relevance, crisp 1 px outline,
  `translucencyByDistance` so far-side points fade slightly (depth cue).

## Testing

- `tests/test_worldview_proxies.py` (or a sibling) gains tests for
  `/api/tles`: shape, count, regime values, TLE line integrity.
- Full suite + ruff + mypy must pass.
- Visual verification via Playwright: FPS readout must be >= 30 at global
  view with satellite layer on; screenshots before/after.

## Non-goals

- No dashboard.html redesign in this pass.
- No WebSocket removal; `/ws/positions` stays for API consumers.
- No re-theming — the amber mil-HUD identity is kept.
