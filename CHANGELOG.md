# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Reference-parity release: the P0 to P2 backlog from the survey of 14 open-source SDA and
globe projects (inventory in `reference/README.md`, gap backlog in `reference/COMPARISON.md`)
is implemented here, plus most of P3 and P4. What is still missing is listed in
`reference/COMPARISON.md` section 8. MIT and Apache sources were adapted with attribution;
AGPL and unlicensed projects were re-implemented from their public design only.

### Added

- Full-catalog screening (`sda.screening`): apogee/perigee shell prefilter, per-step spatial index (scipy `cKDTree` when installed, numpy grid hash otherwise), vectorized `SatrecArray` propagation over one `(jd, fr)` grid; `method="auto" | "allpairs" | "spatial"` on `find_conjunctions`; 5,000 objects screened in 2.9 s with the same candidate set as all-pairs (`scripts/bench_screening.py`)
- Orbit-regime survey (`POST /conjunctions/survey`) with per-regime default thresholds, `regime` on satellite summaries and details, formation-flying exclusion table (`GET /conjunctions/exclusions`, `include_formations`), and a composite 0-100 risk score with a per-component breakdown
- Alfano 2005 erf-strip Pc series with adaptive Simpson intervals and convergence metadata; `pc_by_method` and `POST /pc/compare` report Foster, Chan, Alfano and Monte Carlo side by side
- `pc_usage_checks` returning coded warnings (`LOW_RELATIVE_VELOCITY`, `EXTENDED_ENCOUNTER`, `NPD_COVARIANCE`, `ILL_CONDITIONED_COVARIANCE`, `EXTREME_ASPECT_RATIO`, `CHAN_ASPECT_RATIO`, `HBR_COMPARABLE_TO_MISS`, `PC_DILUTION`), CARA clip-ladder covariance remediation on both the 3x3 and projected 2x2 legs, covariance realism scaling and regime default covariances, Frisbee maximum Pc with a missing covariance (`POST /pc/max-missing-covariance`) and the dilution maximum (`POST /pc/dilution`)
- CCSDS 508.0-B-1 CDM XML reader; `parse_cdm` detects KVN or XML and lands on the same `ParsedCDM`; the writer now states `COLLISION_PROBABILITY_METHOD`; `POST /pc/cdm`
- NASA CARA reference harness (`python -m sda.cara`, now a CI job): 53 CARA CDMs, the `PcMethodTestConjunctions` spreadsheet and the 11 Alfano 2009 literals vendored under NOSA 1.3 in `tests/data/cara/`; Foster inside CARA's tolerance band on 53/53 cases, Alfano 11/11; the 34 adversarial sample CDMs are strict expected failures
- Pass prediction and sensors (`sda.passes`, `sda.sensors`): look angles, rise / max / set passes with sunlit and shadow flags, a bundled sensor network, coverage, geofences and DOP (`POST /passes`, `/lookangles`, `/dops`, `GET /sensors`, `POST /sensors/coverage`, `/sensors/fences`)
- NASA Standard Breakup Model (EVOLVE 4.0) with Gabbard data and a collision-consequence summary (`POST /breakup/simulate`), a state-vector to TLE fitter (`sda.rv2tle`) so fragment clouds can be screened, a re-entry watch list (`GET /reentries`)
- TLE history archive with optional JSONL persistence (`SDA_HISTORY_DIR`), decay-aware maneuver detection with vis-viva delta-V, and historical replay with an optional conjunction screen (`/tle/history/*`, `POST /replay`)
- Operations: Space-Track GP, TIP and public-CDM client with rate throttling (`POST /tle/refresh/spacetrack`, `GET /spacetrack/cdm`), alert rules with webhook delivery (`/alerts/*`), PDF conjunction reports with optional fpdf2 (`POST /report`), fleet screening (`/fleet/*`), a `/doctor` self-check, Kubernetes manifests in `deploy/`
- Agent front door (`POST /agent/chat`) with nine typed tools over an Anthropic, OpenAI-compatible or Ollama provider chain (`SDA_LLM_PROVIDERS`), `GET /agent/tools`, `GET /agent/health`, and an MCP stdio server (`python -m sda.mcp_server`)
- Security posture: loopback bind unless `SDA_HOST` is set, `SDA_PORT`, `SDA_CORS_ORIGINS` allowlist, `SDA_RATE_LIMIT` token bucket on the feed proxies and agent routes, security headers
- WorldView globe: web-worker SGP4 for the whole active catalog, clock-driven time control, share links, covariance shells at TCA, contacts roster, keyboard help, installable static demo (PWA in the static build only), `/api/tles?group=` CelesTrak group fetch, `GET /static/{asset_path}`
- WorldView tactical layer (`static/js/styles.js`, `static/js/tactical.js`): five Cesium `PostProcessStage` GLSL styles replacing the CSS filters (CRT, night vision, FLIR with white-hot / black-hot polarity, noir, snow) with automatic CSS fallback, Cesium bloom as the detection glow, chase / cockpit / orbit-around / preset-tour camera modes, fading trails for selected and tracked objects, a 10 Hz screen-space label arbiter, sensor field-of-regard cones from `GET /sensors` with `POST /sensors/coverage` highlighting; the static build bakes the sensor list
- `scripts/gen_api_table.py` regenerates the README API table from the OpenAPI document; optional extras `screening` (scipy), `reports` (fpdf2) and `full`
- Over 1,300 new offline tests (suite now 1,550+)
- WorldView basemap switch (`I` key, kept in share links as `bm=`): NASA Blue Marble (default), a lit day / night Earth with NASA Black Marble city lights past the terminator, Black Marble alone, and Esri World Imagery for zooming in. All four are keyless and the imagery source is named under the buttons
- Four WorldView sidebar panels, each a plain script under `static/js/` started by the page with one shared context: satellite search by name or NORAD id (`U`), ten live feed layers (launches, space weather, volcanoes, bike share, weather alerts, GDELT, radio, military sites, fires, ships; the last two say which free key they need), pass prediction for an observer picked from the browser location or the view centre, and a chat panel on `POST /agent/chat` that can fly the camera (`Y`)
- `scripts/launch.ps1`: one command that reads every optional key from the Windows user environment, labels the ones that can bill, uses a local Ollama model for the agent, binds the first free port from 8010, restarts its own server in place and opens the globe (`-SetKeys`, `-Status`, `-WhatIf`, `-Port`, `-NoBrowser`). A paid key is stored under a private `SDA_STORED_` name and reaches the server only with `-AllowPaid`; the calling shell's environment is restored on exit
- `SDA_TLE_CACHE_DIR`: opt-in disk copy of the last good catalog. Each CelesTrak group that downloads is saved as `<dir>/<group>.tle`; at startup every `*.tle` file under 72 hours old is loaded, and an older element set never replaces a newer one. With the cache on, a group CelesTrak answered for in the last two hours (with element sets, a 403 or any other reply) is not requested again, so a restart sends it nothing, and the first refresh after such a restart is timed for when the window ends
- A four-colour key under the satellite search box says what the dot colours mean (low, medium, geostationary, high or stretched orbit); a test holds it to the server's `REGIME_COLORS`
- Hooks for globe modules: a per-satellite `hidden` flag every propagation path honours, `refreshSatVisibility()`, the simulation clock and the open conjunction in the shared context, and page events `sda:satellite-selected`, `sda:satellite-deselected`, `sda:conjunction-open`, `sda:conjunction-close` and `sda:catalog-loaded`
- Constellation filter (`static/js/categories.js`, classifier and pills adapted from satlas, MIT): an All pill and fourteen category pills with live counts (Starlink, OneWeb, Kuiper, Qianfan/Guowang, GPS, GLONASS, Galileo, BeiDou, Iridium, Stations, Weather, Geostationary, Debris, Other). Choosing a set hides every other dot; the selected satellite always stays visible
- Live video from the ISS (`static/js/isscam.js`, feed list and embed parameters adapted from osiris, MIT): three public camera feeds in a floating player, a link to each stream on YouTube, and a Find the ISS button. Nothing is requested from YouTube until a feed is pressed
- Satellite search accepts alpha-5 NORAD ids, the letter-and-four-digit form used above 99,999 (`A0001` is 100001; decode adapted from satlas, MIT)
- `THIRD_PARTY.md`: every adapted file with its donor, donor path, commit read and license, the MIT permission notice, vendored data and the services the page loads at run time
- Hover text on the header counters and the view-mode buttons, saying what each counts or looks like and which layer switch fills a counter in
- Conjunction actions (`static/js/conjactions.js`, Pc presentation rules adapted from satguard, MIT): three buttons in the conjunction detail panel that ask the server to compare the four Pc methods (`POST /pc/compare`), list avoidance options smallest delta-V first (`POST /maneuver`) and preview a hypothetical breakup (`POST /breakup/simulate`) with up to 300 fragments drawn on the globe. Each result states what was assumed to build the request; a result for an event that is no longer open is dropped
- `SDA_OFFLINE`: start on the bundled and cached catalogs with no background fetch to CelesTrak or NOAA. The static demo build bakes under it and without a catalog cache, so every build bakes the same catalog and asks the network for nothing
- The test suite cannot reach the network: `tests/conftest.py` refuses every lookup and connection that is not this machine and records the host it refused
- `POST /tle/refresh` reports `groups_asked`, and answers `"status": "waiting"` when every group is still inside CelesTrak's repeat window

### Fixed

- The WorldView globe was covered in "API KEY REQUIRED" text: the CartoDB dark tiles it defaulted to are no longer served without a key. That source is removed
- The agent failed on local Ollama as soon as the model called a tool (HTTP 502): `/api/chat` rejects replayed tool arguments sent as a JSON string, so the Ollama provider now sends them as an object
- One TLE that failed to build in the SGP4 worker shifted every later satellite by one slot, so dots were drawn under a neighbour's name and NORAD id; the worker now keeps a slot for every record
- A restart inside CelesTrak's two-hour repeat-download window dropped the catalog to the bundled demo set (see `SDA_TLE_CACHE_DIR`)
- `GET /doctor` had no deadline; each check now gets 10 seconds and a slow one is reported as a failed row
- The space weather feed returned no Kp and no solar wind. NOAA SWPC now serves the K index as a list of objects and has retired the `plasma-*` solar wind files (HTTP 404); the proxy reads both row shapes and takes speed and density from `rtsw_wind_1m.json`, preferring the spacecraft flagged active
- The left column's last row and the newest line of the event feed sat under the time bar
- Geostationary satellites were coloured as medium orbit below 35,786 km and as high orbit above 35,800 km, so the belt was drawn in three colours. The globe now uses the same 300 km band either side of GEO as the screening code
- `POST /tle/refresh` answered with the refresh time and F10.7 of the previous run, because it read both before refreshing
- A CelesTrak group that answered with anything but element sets or a 403 was asked again on every restart; any reply without elements now rests the group for the window. The fetch also failed without a word: each group that gives nothing now logs why, and a refresh logs how many objects came from how many groups
- The README API table was missing the ten feed routes; regenerated (73 REST operations and 1 WebSocket)
- The test suite was sending real requests to CelesTrak: one test file started the app twelve times with nothing in the way of its startup fetch, 139 requests on every run of that file
- `GET /api/tles?group=` downloaded from CelesTrak on a timer of its own, outside the repeat window. It now serves the refresh's last download (from memory, else the disk copy) and asks only when no reply is on record or the refresh is ten minutes overdue
- Refreshes that overlapped (the startup fetch, the background loop, `POST /tle/refresh`) each requested the same groups. A group with a request on the wire is skipped, and the repeat window is kept in memory as well, so it holds without `SDA_TLE_CACHE_DIR`
- A refresh that brought nothing could put older bundled elements over newer live ones when no cache directory was set
- `last_tle_refresh` moved forward on a refresh that asked nobody, and on one that asked and got no elements back (a machine waking before its network does); it keeps the time of the download the catalog is running on
- `GET /health` reported `auto_refresh_hours` from a literal; it follows the refresh loop's interval
- Every refresh asked CelesTrak for `last-30-days` and got HTTP 404 back: catalog numbers issued since July 2026 have six digits, which a TLE cannot carry, so the group is empty in this format. It is no longer requested and the globe's catalog menu no longer offers it. Objects cataloged since then are absent from every TLE group until the catalog is read as OMM
- Space weather read SWPC fill values (-9999.9 and the like) and booleans as measurements. Kp, wind speed and density are held to physical ranges, and the standby spacecraft is used once the active one's newest reading is more than ten minutes behind it
- The static demo build wrote `/api/config` as served, so a map key in the build environment would have been published with the demo. Every field ending in key, token or secret is now nulled in the baked file
- A `/doctor` check that never returned cost one more thread on every request. A check still running from an earlier request is joined, not started again, and is reported as such once past its deadline
- `scripts/launch.ps1` passed an `SDA_LLM_PROVIDERS` value naming a paid provider through to the server without `-AllowPaid`; without that switch the list is now always `ollama`
- With the chart library blocked, opening a second conjunction left `getConjunction()` on the first while the panel showed the second. The open event is set before the charts are drawn, and a chart that cannot draw says so in its box
- `refreshSatVisibility()` drew objects with no position (not yet propagated, or with no SGP4 solution) at 0N 0E
- While imagery loaded the globe was a black disc on a black sky; its base colour is now a deep blue. The small dim labels in the default view mode went from 3.1:1 to 4.5:1 contrast
- Browser shortcuts fired the globe's single-key shortcuts (Ctrl+R also pinned a satellite, Ctrl+Shift+I also changed a layer); the key handler now ignores Ctrl, Alt and Meta combinations
- The WorldView Pc trend chart drew a random series; it now reads `POST /api/conjunction-pc-history` and says so when the history is empty or all zero
- `/ws/positions` was never consumed by the globe; it now drives tracked objects with exponential-backoff reconnect
- A supplied Pc indented the whole generated CDM by eight spaces
- Very large SGP4 positions from decaying objects silently degraded the spatial index to all-pairs

### Notes

- The Chan series is exact only for isotropic covariances; measured against the CARA set its error grows with aspect ratio (3% up to 10:1, near 100% above 100:1). Foster or Alfano is the reported value; the `CHAN_ASPECT_RATIO` warning fires when the reduction-error estimate exceeds 5%
- Deliberately not imported: keeptrack missile simulation and Vimpel feed, worldmonitor desktop/mobile packaging, messaging, crypto and news video, i18n, gamepad, screen recorder, and the paid realtime voice path (the agent panel's mic button uses the browser's own speech recognition and the replies are text)
- Not yet imported, tracked in `reference/COMPARISON.md` section 8: the CARA 3D-Nc, Elrod and SDMC confidence-bound methods and ephemeris Pc; the worldmonitor cable and datacenter layers; the CCTV viewshed and 3D aircraft models; the gods-eye-view Anime style; the satguard Clohessy-Wiltshire tradespace; the spacetech-ssa ML residual model and CZML export

## [0.3.0] - 2026-07-31

### Added

- Full-covariance collision probability: Foster 2D quadrature (`pc_foster`), Chan analytic series (`pc_chan`), and 2D Monte Carlo (`pc_monte_carlo_2d`) over real 3x3 position covariances, with RTN-to-ECI covariance rotation and encounter-plane projection (`compute_collision_probability_full`)
- CCSDS CDM ingestion: KVN parser (`parse_cdm`) honoring CCSDS 508.0-B-1 default units with bracketed-unit overrides, and Pc computed from the message's per-object RTN covariances (`compute_pc_from_cdm`); CDM support is now two-directional
- `POST /conjunctions/cdm/ingest` endpoint returning parsed CDM fields plus Foster and Chan Pc, the stated Pc, and any assumptions made
- Cross-method Pc validation suite (`python -m sda.validation`, non-zero exit on failure): closed form vs. Chan series vs. Foster quadrature vs. Monte Carlo vs. legacy pipeline across 11 isotropic/anisotropic scenarios; max relative error between independent methods 3.7e-6
- 42 new tests (probability methods, CDM round-trip and ingestion, validation suite)

### Notes

- The Chan series is evaluated in a cancellation-free summation order; the textbook order loses all precision in the deep tail (verified: 33% error at an 8-sigma miss before the reorder)
- Chan vs. Foster anisotropic disagreement (0.2% at 3:1 aspect, 13% at 10:1) is the documented equivalent-area approximation error; Foster is the primary reported value

## [0.2.0] - 2026-07-05

### Added

- Modular FastAPI routers (`sda.routes`): satellites, conjunctions, analysis, system, worldview (26 REST endpoints + WebSocket)
- Shared physical constants module (`sda.constants`) with values matching SGP4's WGS72 gravity model
- WorldView CesiumJS globe endpoints (satellite positions/orbits, flights, earthquakes, conjunction overlays)
- Docker and docker-compose deployment with container health checks
- GitHub Actions CI: tests, coverage, lint (ruff), type check (mypy)
- Community and packaging files: CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CITATION.cff, LICENSE, Makefile
- Propagation benchmark suite (`benchmarks/`)
- Extended test suites for conjunction, decay, maneuver, and probability modules
- `py.typed` marker for downstream type checking
- `__version__` in `sda` package, surfaced in the FastAPI app

### Changed

- Collision probability now uses TLE-age-scaled position covariance (Vallado 2013, Ch. 9) instead of a fixed 50 m sigma, so stale TLEs no longer produce overconfident Pc values
- Earth constants aligned to SGP4 WGS72 exactly: equatorial radius 6378.135 km, gravitational parameter 398600.8 km³/s² (removes a ~7 km bias in decay altitude estimates)

### Fixed

- Cross-track maneuver delta-V divided by orbital radius instead of multiplying by mean motion, producing values wrong by orders of magnitude
- Restored the `run()` entry point and `__main__` block so `sda-server`, `python -m sda.api`, `make serve`, and the Docker CMD actually start the server
- Dockerfile build order: source and README are copied before `pip install`, so the image builds

## [0.1.0] - 2026-03-22

### Added

- SGP4 orbital propagation with WGS72 gravity model and vectorized NumPy batch processing
- Two-phase conjunction detection pipeline (coarse 60s screening, fine 1s refinement)
- Five-level risk classification (CRITICAL / HIGH / MODERATE / LOW / NEGLIGIBLE)
- Collision probability computation using 2D Gaussian B-plane projection
- Maneuver planning with along-track, cross-track, and radial delta-V options
- Monte Carlo miss distance analysis with Gaussian position perturbations
- Orbital decay estimation using Harris-Priester atmospheric model
- CCSDS 508.0-B-1 Conjunction Data Message generation
- Interactive 3D Plotly visualization with Earth, orbit traces, and screening volumes
- CesiumJS globe with CRT, NVG, and FLIR visual filters
- FastAPI service with REST endpoints and WebSocket position streaming
- Async CelesTrak TLE ingestion with multi-group fetch
- NOAA space weather integration (F10.7 solar flux)
- Embedded web dashboard with dark mode

[Unreleased]: https://github.com/jdardash/space-collision-predictor/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jdardash/space-collision-predictor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jdardash/space-collision-predictor/releases/tag/v0.1.0
