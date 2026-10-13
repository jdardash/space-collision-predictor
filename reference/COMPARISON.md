# Capability comparison: this repo vs 14 reference projects

Date: 2026-09-02. Method: every reference repo was cloned at depth 1 into
`reference/` (pinned commits in [README.md](README.md)), inventoried by
read-only agents against one fixed template with `path:line` citations, and
the load-bearing claims about this repo were re-checked by hand (grep and
file reads, cited below). Nothing in any reference repo was modified.

This document answers two questions the user asked:

1. Does this repo do everything gods-eye-view does, and more?
2. Against the wider field of "open-source Palantir" style OSINT globes and
   space situational awareness (SSA) tools, is this repo as good or better?

Short answers: (1) No. (2) On conjunction analysis, yes, against every repo
except NASA CARA (a reference implementation, not a product) and
keeptrack.space in breadth. On the globe, data layers, AI and operational
polish, no. Details and evidence follow.

## 1. gods-eye-view vs this repo

gods-eye-view (bilawalsidhu, 15.9k stars, MIT, v0.1.1 at 65bc522) is an
OSINT globe: it has no orbital analysis at all. A grep of its `src/` for
conjunction, collision probability, covariance, CDM, maneuver and decay
returns only label-declutter code. This repo's WorldView page was modelled on
it, so the comparison is between its whole product and our globe page plus
our analysis engine.

### 1.1 What this repo does that gods-eye-view does not

Everything below `src/sda/` outside `templates/` and `routes/worldview.py`:

| Capability | This repo | gods-eye-view |
| --- | --- | --- |
| SGP4 propagation server-side, vectorized NumPy, WGS72 | `propagator.py` | none (client satellite.js only) |
| Conjunction screening, two-phase 60 s coarse then 1 s fine | `conjunction.py` | none |
| Collision probability, Foster quadrature + Chan series + Monte Carlo | `probability.py:315,370,421` | none |
| Covariance: TLE-age scaled, plus full 3x3 RTN from CDMs | `probability.py`, `cdm.py` | none |
| CCSDS CDM generation and KVN ingestion | `cdm.py` | none |
| Maneuver delta-V planning | `maneuver.py` | none |
| Atmospheric decay with F10.7 | `decay.py` | none |
| Cross-method Pc validation as a CI gate | `validation.py`, `.github/workflows/ci.yml` | none |
| REST API (27 endpoints) + WebSocket | `routes/` | Vite dev-middleware proxies only |
| Plotly 3D analysis view | `visualization.py` | none |
| Conjunction overlay on the globe (globe data + B-plane) | `routes/worldview.py` | none |
| Dockerfile + docker-compose | repo root | none (Pinokio + npm) |
| Static offline demo build published to GitHub Pages | `.github/workflows/pages.yml` | none |
| Python type checking in CI (mypy) | `ci.yml` | no lint or typecheck script |

### 1.2 What gods-eye-view does that this repo does not

| Area | gods-eye-view | This repo |
| --- | --- | --- |
| AIS ships (AISStream, key) | yes | no |
| NASA FIRMS fires (key) | yes | no |
| Radio Browser stations | yes | no |
| GBFS bikeshare | yes | no |
| Launch Library 2 upcoming launches | yes | no |
| OSM military installations | yes | no |
| Bundled datacenters, dams, submarine cables | yes | no |
| Flights from two feeds (OpenSky + adsb.lol) | yes | OpenSky only, 15 s |
| Satellite pass predictor | ISS only, `src/data/issPass.js` | none |
| Starlink DENSE mode, GMST-realigned orbit rings, satellite.js v6 | yes | ~1,400 objects, satellite.js 5.0.0, orbit rings from `/api/satellite-orbits` |
| Visual styles | GLSL post-process CRT, NVG, FLIR, Noir, Snow, Anime | CSS `filter:` on the Cesium canvas, CRT/NVG/FLIR only (`worldview.html:36-69`) |
| Detection overlay, bloom, label arbiter | yes | no |
| 3D aircraft models, aircraft trails, cockpit view | yes | no (points) |
| Contacts roster, CCTV viewshed | yes | no |
| Voice annotations, scene director, first-run missions | yes | no |
| Share links (URL state) | yes | no |
| Global context mode, HUD scene summary (OpenAI) | yes | HUD without AI |
| OpenAI Realtime voice agent with 28 tools | yes | none |
| Analyst engine | yes | none |
| Google Places search | yes | Nominatim geocode |
| Setup doctor, in-app provider settings, Pinokio one-click install | yes | pip/venv, Docker |
| Security: localhost bind, per-IP rate limits, server-side key broker with SSRF mitigation | yes | binds 0.0.0.0 (`api.py:240`), CORS `*` (`api.py:213`), no rate limiting |
| Tests | 165 `*.test.mjs` + 39 Puppeteer `scripts/qa-*.mjs`, CI on Node 24/26 plus a Windows onboarding job | 219 `def test_` in 22 files (227 collected), CI pytest 3.11-3.13 |
| Docs | README, DATA_SOURCES, SECURITY, TESTING, CONTRIBUTING, CHANGELOG, CURRENT-STATE (273 KB), KNOWN-ISSUES, PERFORMANCE | README, SECURITY, CONTRIBUTING, CHANGELOG, handoffs |
| Use-limitation statement | no named-person search or face recognition; not for navigation | README Disclaimer (`README.md:266-274`) |

### 1.3 Verdict on question 1

We do not do "everything and more". We do a different, deeper thing
(conjunction assessment) that gods-eye-view does not attempt, and our globe
page is a reduced copy of theirs: 6 of its 13+ live layers, CSS filters
instead of shaders, no voice or AI layer, no pass prediction, no share links,
and a weaker default network posture. Roughly: we cover about half of its
globe feature list and it covers none of our analysis feature list.

## 2. The two defects found in our WorldView while comparing

These were found by reading our own code during the comparison and are
reported here, not fixed, because the request was an assessment.

1. **The Pc evolution chart is fabricated.** `renderPcEvolutionChart` in
   `src/sda/templates/worldview.html:2868-2878` fills `pc_history` with
   `Math.random()` noise around the current Pc whenever the array is empty,
   and it is always empty: `pc_history` is only referenced at lines 2821,
   2868 and 2869, and the client never calls `POST
   /api/conjunction-pc-history` (`src/sda/routes/worldview.py:845`), which is
   the endpoint that would supply real history. Every trend line a user sees
   on that panel is synthetic.
2. **`/ws/positions` has no consumer.** The WebSocket at
   `src/sda/routes/worldview.py:173-200` is advertised in README and
   CLAUDE.md, but a grep for `ws/positions` and `new WebSocket` across
   `worldview.html` and `dashboard.html` returns nothing. The globe polls
   `/api/tles` instead.

Posture, not a defect: `allow_origins=["*"]` (`src/sda/api.py:213`) and
`host="0.0.0.0"` (`src/sda/api.py:240`) are the defaults. gods-eye-view
binds localhost and rate-limits per IP by default; a globe page that proxies
third-party feeds benefits from the same.

## 3. NASA CARA: the Pc reference, and the test vectors we can use

`nasa/CARA_Analysis_Tools` (MATLAB, NASA Open Source Agreement v1.3 via four
`NOSA_GSC-*.pdf` files, no LICENSE file) is not a product but the public
release of the Conjunction Assessment Risk Analysis team's methods. Our README
names it as the next validation tier. What it has that we do not:

| CARA capability | Path | Ours |
| --- | --- | --- |
| 2D Pc, Foster 1992 with circle, square and equal-area HBR | `DistributedMatlab/ProbabilityOfCollision/Pc2D_Foster.m` | Foster quadrature, circle only |
| Vectorized 2D circle Pc, Gauss-Chebyshev 64th order default | `PcCircle.m`, `PcCircleWithConjData.m` | none (scalar loop) |
| Elrod 2019 error-function quadrature | `PcElrod.m` | none |
| 2D-Nc and 3D-Nc curvilinear methods (Hall 2021) for slow or extended encounters | `Pc2D_Hall.m`, `Pc3D_Hall.m` | none; 2D linear only |
| Tiered orchestrator with usage-violation checks | `PcMultiStep.m`, `PcMultiStepWithSDMC.m` | `compute_pc_for_conjunction` picks Foster or Chan, no validity checks |
| Simple Dynamics Monte Carlo with confidence bounds (Linux MEX) | `Pc_SDMC.m` | 2D Gaussian sampling, no CI |
| Covariance cross-correlation via CDM DCP fields (Casali 2018) | `Utils/CovarianceDeCorrelationCode/Pc_CovXCorr.m` | none |
| Max-Pc when one covariance is missing; dilution analysis | `Maximum2DPc/FrisbeeMaxPc.m`, `DilutionMaxPc.m` | dilution regression test only |
| Non-positive-definite covariance remediation | `Utils/RemediateCovariance2x2.m`, `CovRemEigValClip.m` | none |
| Covariance realism from residuals | `DistributedMatlab/CovarianceRealism/` | none |
| Credibility / upper Pc bound (Delande 2023) | `CredibilityOfCollision/Uc2D_Credibility.m`, `Uc3D_Credibility.m` | none |
| Collision consequence: debris piece count, mass from RCS | `CollisionConsequence/` | none |
| OD quality assessment of a CDM | `ODQualityAssessment/CDM_ODQualityAssessment.m` | none |
| Ephemeris-based Pc over a whole trajectory | `EphemerisPc/src/EphPc.m` | none |
| RTN, VNB and equinoctial covariance transforms with Jacobians | `Utils/CovarianceTransformations/`, `Utils/OrbitTransformations/` | RTN to ECI only |

What we have that CARA does not: a TLE-age-scaled covariance model (CARA is
CDM/OD-covariance only; no "TLE" hits under `ProbabilityOfCollision` or
`Utils`), any propagation or screening, an API, a UI. CARA has no maneuver
planning or red/yellow threshold code either; thresholds live in its
reference papers.

Alfano, Patera, Serra and Coppola are present as reference PDFs and as test
data, not as functions. There is no `PcAlfano`.

### 3.1 Test vectors readable without MATLAB (verified 2026-09-02)

| Set | Path | Format | Count | Validates |
| --- | --- | --- | ---: | --- |
| Real conjunction CDMs with CARA's own Pc embedded (`COLLISION_PROBABILITY`, `COLLISION_PROBABILITY_METHOD = FOSTER-1992` at lines 16-17 of each file) | `DataFiles/PcTestCaseCDMs/*.cdm` | KVN text | 53 | our CDM ingest and Foster path end to end |
| Reference Pc under five methods per CDM, with SDMC confidence bounds and usage-violation flags. Columns: Primary, Secondary, HBR_m, Pc2D, Nc2D, Nc3D, Pc3DCoppola, PcSDMC, PcSDMCLo, PcSDMCHi, Violations*, NhitSDMC, NtotSDMC, Conjunction_ID | `DataFiles/PcTestCaseCDMs/CARA_PcMethod_Test_Conjunctions.xlsx` | xlsx, one sheet | 53 rows | Foster vs Pc2D, Monte Carlo vs PcSDMC interval, where 2D linear diverges from Nc3D |
| Adversarial edge cases: Alfano, Omitron (high Pc, max radial/in-track/cross-track sigma, min miss, min relative velocity, non-PD covariance, 3D-Nc), single-covariance, Frisbee | `DataFiles/SampleCDMs/*.cdm` | KVN text | 34 | robustness of the full-covariance path |
| Alfano 2009 AAS 09-233 benchmark cases at epoch and at TCA | `DataFiles/AlfanoInputData/Alfano_2009_Test_Cases/` | xls + xlsx | 12 | Monte Carlo vs analytic |
| Alfano expected Pc as literals (`1.46749549E-01`, `6.22226700E-03`, ...) plus Omitron literals | `DistributedMatlab/ProbabilityOfCollision/UnitTests/Pc2D_Foster_UnitTest.m:31-42`, `PcElrod_UnitTest.m:14-33` | MATLAB source, plain text | 11 + Omitron | Foster and Chan directly |
| Alfano 3D-Nc outputs | `Pc3D_Hall_Utils/Output_AlfanoTestCases_Pc3D/A09TestCases_01_12_Distributed.csv` | CSV | 12 | cross-check |

The README in `DataFiles/PcTestCaseCDMs/` (lines 35-49) notes that CDM
states are given at a millisecond-rounded TCA and must be re-propagated to
the true TCA before Pc is computed; `Pc2D_FromCDM.m` and
`ComparePc2D_to_CARAValues.m` (tolerance bands from 1e-15 to 1e-3 at lines
17-24) show the exact procedure. NOSA v1.3 grants use, modification,
distribution and redistribution with attribution and inclusion of the
agreement text, no copyleft, so these files can be vendored as test fixtures.

## 4. The field: 14 reference repos on fixed axes

Counts are grep counts from the pinned clones (test files or `def test_`
functions as noted), not claims from READMEs. "none" means the agent grep for
conjunction, probability, covariance, CDM, maneuver and decay found nothing.

| Repo | Screening | Pc and covariance | CDM | Maneuver | Decay | Satellite viz | Pass prediction | Other live layers | AI | Tests | CI gate | License | Maturity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **this repo** | all-pairs two-phase, 60 s then 1 s (`conjunction.py:153`) | Foster, Chan, Monte Carlo; TLE-age and full RTN cov | KVN read + write | delta-V planning | F10.7 lifetime model | Cesium, ~1,400 client, Plotly 3D | none | 6: flights, military, quakes, traffic, CCTV, geocode | none | 22 files, 219 fn | ruff, mypy, pytest 3.11-3.13, Pc cross-method gate | MIT | usable, v0.3.0 |
| gods-eye-view | none | none | none | none | none | Cesium, ~840 client, GLSL styles, orbit rings | ISS only | 13+ | OpenAI Realtime voice, 28 tools, HUD summary | 165 files + 39 Puppeteer QA | Node 24/26 + Windows onboarding | MIT | production, v0.1.1 |
| keeptrack.space | two-phase in worker via ootk; RPO all-vs-all with golden-section TCA; hosted SOCRATES feed | Pc from ootk submodule (empty in clone); TLE-age capped cov; hosted covariance.json; 3D ellipsoids | none | ML detection only (OrbitGuard) | Space-Track TIP feed, no model | own WebGL, full catalog + debris + Vimpel, 9 workers, WASM SGP4 | multi-sensor look angles, DOPS, fences, best pass | breakup model (EVOLVE 4.0), missiles, launches, sensors | none | 394 test + 85 spec files | lint, typecheck, coverage gate, CodeQL, Playwright | AGPL-3.0 | very mature, v13.10.0 |
| worldmonitor | none | none | none | none | none | globe.gl + deck.gl, curated list, satellite.js 3 s | stubbed TODO (`src/services/satellites.ts:1-10`) | 58 layers, 38 service domains | multi-provider LLM chain, MCP server | 1,761 files + 52 e2e | 42 workflows | AGPL-3.0 | very mature, v2.10.0 |
| Shadowbroker | none | none | none | TLE-history change heuristic | mean-motion anomaly flag | MapLibre 2D, 2,000+ server sgp4, 24 h | none (SatNOGS, TinyGS) | 60+ feeds | HMAC agent command channel, no bundled LLM | ~3,099 py fn + 90 FE files | ruff, black, pytest, eslint, vitest; no mypy | AGPL-3.0 | sprawling prototype, v0.9.84 |
| osiris | none | none | none | none | none | MapLibre 2D, server satellite.js | none | ~12 | Gemini briefings | 35 files, 515 cases | Docker build only | MIT | prototype, v0.1.0 |
| ThingsInSpace | none | none | none | none | none | raw WebGL, 15,151 static 2019 snapshot, worker | none | 0 | none | 0 | none | none | abandoned 2019 |
| satvis | none | none | none | none | none | Cesium, 12,000+ client worker, OMM JSON, PWA offline | full: elevation and swath, notifications | 0 | none | 47 files, ~798 cases | lint, vitest, build | MIT | production tracker |
| CARA | n/a (methods only) | Foster, Circle, Elrod, 2D-Nc, 3D-Nc, SDMC, cross-correlation, max Pc, dilution | KVN read | none | none | none | none | n/a | none | ~60 unit test methods + 53 CDM oracles | none (MATLAB) | NOSA 1.3 | reference |
| spacetech-ssa | KD-tree radius query, no Pc | none; XGBoost residual on Cowell J2 | none | none | none | Cesium CZML | none | 0 | none | 5 fn | none | Apache-2.0 | research toy, 2021 |
| satlas | none (V2 roadmap) | none | none | none | none | Three.js, 30k client worker, skyfield server | full, with visibility score | clouds, star field | Claude agent, 6 typed tools | 183 FE + 119 py | lint, typecheck, pytest, Docker, ECS deploy; Stryker config | MIT | near production |
| satguard | KD-tree, 60-120 s, SatrecArray full catalog in 29 s | Foster, Chan, Alfano; regime default cov; Hejduk realism | write only | CW tradespace, Hill-validated | none | Cesium, 30k+ client | none | 0 | none | 21 files, 237 fn | mypy, ruff, pytest 3.12/3.13 | MIT | beta, v1.0.0 |
| orbveil | shell prefilter + KD-tree, bisection TCA | Foster + Monte Carlo; cov from CDM or user only | KVN + XML read | none (by design) | none | none | none | 0 | none | 8 files, 143 fn | none in clone | Apache-2.0 | pre-alpha |
| OrbitVoyage | O(n^2) at 30 s, 500-object cap | ad-hoc Gaussian in miss distance | none | cosmetic formula | none | Three.js textureless sphere | none | 0 | none | 0 | none | none | scaffold |
| worldview (shawn14) | none | none | none | none | none | Cesium + Google 3D tiles, GLSL CRT/NVG/FLIR | none | military, flights, quakes, NYC cams | none | 0 | none | none | toy |

## 5. Are we as good or better? Verdict by axis

**Conjunction analysis engine: yes, against every product-style repo.** Ten of
the fourteen have no Pc at all. Of the four that do, OrbitVoyage has a single
Gaussian formula, orbveil has Foster plus Monte Carlo and no maneuver or decay
by design, keeptrack keeps its Pc in a submodule that is empty in this clone
and its headline numbers come from a hosted, login-gated SOCRATES feed, and
CARA is a method library rather than a product. satguard is the only real
peer, and it beats us on three specific points:

- an Alfano series as a third Pc method (we have two, plus Monte Carlo);
- a KD-tree spatial index with vectorized `SatrecArray`, giving a full-catalog
  screen in 29 s, where our coarse phase is all-pairs `itertools.combinations`;
- a Clohessy-Wiltshire maneuver tradespace validated against Hill integration.

We beat satguard on Monte Carlo, atmospheric decay, CDM ingestion, the
full-covariance Pc path, the 1 s fine phase and the WebSocket. Against CARA we
are behind on everything in section 3 except TLE-based covariance.

**Satellite visualization: no.** keeptrack, satvis and satlas propagate 12k to
30k objects in a web worker; we propagate ~1,400 on the main thread. Three
repos have real pass prediction (satvis, satlas, keeptrack with multi-sensor
look angles) and we have none. gods-eye-view and shawn14/worldview use GLSL
post-process shaders where we use CSS filters.

**OSINT data layers: no.** Six layers against 13+ (gods-eye-view), 58
(worldmonitor), 60+ (Shadowbroker) and ~12 (osiris). We are ahead only of the
pure SSA tools, which have zero.

**AI and agent features: no.** Five repos ship something (gods-eye-view
voice, satlas Claude agent, osiris Gemini, worldmonitor LLM chain and MCP
server, Shadowbroker agent channel). We ship nothing, although our 27
endpoints are exactly the surface a satlas-style typed-tool agent needs.

**Engineering quality: mid-pack.** Our CI gate (ruff, mypy, three Python
versions, Pc cross-method validation) is stronger than the eight repos with
no enforced gate at all (osiris, orbveil, OrbitVoyage, shawn14/worldview,
ThingsInSpace, spacetech-ssa, CARA, and Shadowbroker on typing). Test volume
is well below gods-eye-view, keeptrack, worldmonitor, satvis, satlas and
Shadowbroker.

**Security posture: no.** gods-eye-view binds localhost and rate-limits per
IP, worldmonitor refuses to start without generated secrets, Shadowbroker
SSRF-guards its proxies. We bind 0.0.0.0 with CORS `*` and no rate limiting.

## 6. Gap backlog, prioritized

Each item names the reference that shows how it is done. AGPL repos
(keeptrack, worldmonitor, Shadowbroker) are design references only; MIT and
Apache repos can be copied with attribution; CARA data can be vendored with
the NOSA text; the three unlicensed repos cannot be copied.

| Pri | Gap | Reference | Notes |
| --- | --- | --- | --- |
| P0 | Fabricated Pc trend chart | our `worldview.html:2868-2878` | wire `POST /api/conjunction-pc-history` or remove the chart; a synthetic trend on a safety panel is worse than no panel |
| P0 | `/ws/positions` unused | our `routes/worldview.py:173-200` | consume it from the globe or delete it and the README and CLAUDE.md rows |
| P1 | Network defaults | gods-eye-view `server/`, worldmonitor `SELF_HOSTING.md` | bind 127.0.0.1 unless `SDA_HOST` is set, CORS allowlist, per-IP rate limit on the `/api/*` feed proxies |
| P1 | External Pc validation tier | CARA, section 3.1 | harness over 53 CDMs + xlsx, 11 Alfano literals, 34 adversarial CDMs; report Foster vs Pc2D and where 2D linear diverges from Nc3D |
| P2 | Full-catalog screening | satguard `screen/vectorized.py`, orbveil shell prefilter | apogee/perigee shell prefilter then KD-tree per step; keep the 1 s fine phase |
| P2 | Third Pc method and validity checks | satguard `assess/alfano.py`, CARA `PcMultiStep.m` | Alfano series; usage-violation flags for slow encounters |
| P2 | Covariance realism and NPD remediation | satguard `covariance/realism.py`, CARA `Utils/RemediateCovariance2x2.m` | |
| P2 | CDM XML reader | orbveil `data/cdm.py` | we read KVN only |
| P2 | Pass prediction | satvis `src/modules/PassPredictor.ts`, satlas `apps/orbital/passes.py` | ground station to azimuth, elevation, rise and set; one endpoint plus a globe panel |
| P2 | Worker-based client propagation of the full active catalog | satvis `src/modules/util/sgp4Worker.ts` | lifts the ~1,400 cap without touching the server |
| P3 | Share links (URL state) | satvis ADRs, gods-eye-view | |
| P3 | Three cheapest missing layers | gods-eye-view DATA_SOURCES | AIS (AISStream, key), FIRMS fires (key), Launch Library 2 (keyless) |
| P3 | GLSL post-process styles | gods-eye-view (MIT); shawn14/worldview `src/shaders/*.glsl` is unlicensed, design only | replace the CSS filters |
| P3 | Agent front door over the REST API | satlas `api/chat.ts:441-524` | typed tools: run conjunction, explain Pc, fly to event |
| P4 | Breakup model, sensor network, RPO survey | keeptrack (AGPL, design only) | outside current scope |

## 7. Method and limits

Inventories were produced by five read-only agents over the pinned clones,
one fixed template each, with `path:line` citations. Claims that drive the
verdicts were re-checked by hand: our screening loop, Pc entry points, test
counts, style implementation and README disclaimer; the CARA CDM count,
spreadsheet, Alfano literals and license files; the keeptrack empty submodule
and worker imports. No reference project was built or run. Star counts are as
of 2026-09-02. Two agent figures were corrected on inspection: CARA
`SampleCDMs` holds 34 files, not 30, and the "29,790 objects under 10 s"
figure for orbveil came from my brief, not its repo (its README says 30,070
objects in 15-20 s on a Jetson).

## 8. Status after the implementation wave (verified 2026-09-02, 16:20 PDT)

Branch `feat/reference-parity`, uncommitted on top of `e46b4df`. Every number in
this section was produced in this session by running the command shown, and every
agent claim that drives a verdict was re-checked by opening the cited line. Line
numbers in `worldview.html` move while other sessions edit it; the function names
are the stable reference.

### 8.1 Evidence

| Check | Command | Result |
| --- | --- | --- |
| Test suite | `pytest tests/ -x -q` | 875 passed, 1 skipped, 29 xfailed in 51 s |
| Lint and types | `ruff check src/ tests/`, `mypy src/sda` | both clean; 4 UP017 hits remain in `scripts/generate_preview.py`, outside the CI scope |
| CARA harness | `python -m sda.cara` | 53/53 CDMs and 11/11 Alfano 2009 cases pass; the harness itself reports 29 of 53 CDMs as outside the 2D method's validity |
| Cross-method gate | `python -m sda.validation` | passes |
| API surface | `python scripts/gen_api_table.py` | 63 REST endpoints + 1 WebSocket; README table in sync |
| Screening at scale | `python scripts/bench_screening.py` (numpy grid backend, scipy not installed) | 5,000 objects: all-pairs 225.0 s, spatial 15.3 s, identical 6,922 candidates. 12,000 objects: all-pairs 480.6 s, spatial 14.2 s, identical 41,362 candidates |

### 8.2 Section 6 backlog, item by item

| Pri | Item | Status | Evidence |
| --- | --- | --- | --- |
| P0 | Fabricated Pc chart | done | `routes/worldview.py:1100` `conjunction_pc_history`, fetched by the globe's Pc panel |
| P0 | `/ws/positions` never consumed | done | `routes/worldview.py:280` `websocket_positions`; `worldview.html` `new WebSocket(serverTrackUrl())` |
| P1 | Network defaults | done | `ratelimit.py:325` `bind_host` (127.0.0.1), `:351` `cors_origins`; middleware stack `api.py:229-246` |
| P1 | CARA validation tier | done | `sda/cara.py:590` `find_true_tca`, `:869` `compare_all`; fixtures `tests/data/cara/` under NOSA 1.3; CI job |
| P2 | Full-catalog screening | done | `screening.py:233` `grid_pairs_within`, `:369` `active_spatial_backend`; bench above |
| P2 | Alfano and validity checks | done | `probability.py:888` `pc_alfano`, `:1205` `pc_usage_checks` (8 codes) |
| P2 | Covariance realism and NPD | done | `probability.py:937` `remediate_covariance`, `:1526` `covariance_realism_scale`, `:1011` `frisbee_max_pc`, `:1078` `dilution_max_pc` |
| P2 | CDM XML | done | `cdm.py` XML reader, `POST /pc/cdm` |
| P2 | Pass prediction | done | `passes.py:242` `look_angles`, `:604` `predict_passes`; `sensors.py:460` `fence_crossings`, `:664` `compute_dop` |
| P2 | Worker propagation | done | `templates/static/js/sgp4-worker.js` |
| P3 | Share links | done | `worldview.html` `encodeShareState` / `parseShareHash` |
| P3 | AIS, FIRMS, Launch Library 2 layers | open | no code; no key path either (CLAUDE.md corrected) |
| P3 | GLSL styles | done, 5 of 6 | `static/js/styles.js:234` ORDER: crt, nvg, flir, noir, snow; gods-eye-view's Anime style not ported |
| P3 | Agent front door | done | `agent.py:882` `TOOL_SPECS` (9 tools), `mcp_server.py` |
| P4 | Breakup, sensors, RPO | done | `breakup.py:465` `simulate_breakup`, `:620` `gabbard_data`; `sensors.py:349-664`; `POST /conjunctions/survey` |

### 8.3 Verdict per reference

Counts are present / partial / absent over the capabilities each audit enumerated.
The globe family (gods-eye-view, worldview, ThingsInSpace, OrbitVoyage) and the
tracker family (keeptrack, satvis, satlas) were audited by read-only agents and then
spot-checked by hand; the engine family (CARA, satguard, orbveil, spacetech-ssa) and
the OSINT family (worldmonitor, Shadowbroker, osiris) were audited by hand in this
session after the agents assigned to them were cut off by a rate limit.

| Reference | Present / partial / absent | Ours ahead on | Theirs ahead on |
| --- | --- | --- | --- |
| NASA CARA | 2D methods, usage checks, remediation, realism, Frisbee, dilution, consequence: present. Elrod, PcCircle Gauss-Chebyshev, square and equal-area HBR, 2D-Nc and 3D-Nc Hall, PcMultiStep orchestration, SDMC confidence bounds, DCP cross-correlation, Delande credibility, OD quality, ephemeris Pc, VNB and equinoctial transforms: absent | runnable service, CDM XML read, globe, tests reproducing CARA's own numbers | Pc method depth; 3D-Nc matters most because CARA flags 29 of 53 real CDMs as outside 2D validity |
| satguard | all engine modules present (screening, Foster/Chan/Alfano, realism, CDM writer, fleet, history and replay, alerts, PDF reports, Space-Track); absent: Clohessy-Wiltshire tradespace (`maneuver/cw.py`), general CLI, conjunction browser with heatmap | CARA harness, usage checks, XML CDM, passes and sensors, breakup, agent, MCP | CW tradespace, CLI, web conjunction browser |
| orbveil | formations, composite risk, screening, CDM XML, Space-Track: present; absent: Python client library | everything above the screening layer | packaged API client |
| spacetech-ssa | conjunction search and Space-Track ETL present (ours two-phase with spatial index); absent: CZML export, ML residual orbit model | screening scale, Pc, CDM | CZML for Cesium ingest, ML residual model |
| gods-eye-view | 9 / 11 / 25 | SSA math the globe visualizes, full-catalog worker SGP4, CI and tests, uniform security middleware | OSINT layer breadth (AIS, radio, bikeshare, fires, launches, installations, cables), CCTV viewshed and gizmo, voice agent and annotations, Anime style, 3D aircraft models, one-click installer |
| worldview (shawn14) | 8 / 3 / 7 | SSA domain, CI, tests, rate limit and bind posture, worker propagation | named camera presets, geopolitical alert zones, live camera-feed panel, country labels |
| ThingsInSpace | 6 / 2 / 1 (2 unknown) | backend, analysis, live refresh, share links, camera modes | satellite name and NORAD search box, constellation filter checkboxes (ours loads CelesTrak groups instead) |
| OrbitVoyage | 14 / 2 / 8 (2 unknown) | entire assessment stack; their alerts module is an empty stub and their Pc is one ad-hoc formula | NORAD search overlay, playback-rate scrubber, idle auto-hide panels, WebSocket push of conjunction events |
| keeptrack.space | 21 / 14 / 19 | Pc rigor, CDM interop, CARA harness, MCP server, no hardcoded third-party credentials | WebGL renderer scale, WASM SGP4, missile modelling (deliberately excluded), RCS and magnitude estimators, polar plots, scenario persistence, i18n, CodeQL and Dependabot, Playwright e2e |
| satvis | 6 / 10 / 9 | Pc and conjunction analytics, CDM, breakup, decay, agent, MCP | AR sky view, real push notifications, declarative satellite metadata with SATCAT merge, ADRs, offline imagery pipeline, serverless deploy |
| satlas | 9 / 8 / 10 | conjunction analysis (their roadmap item), 63 endpoints vs 6, MCP, agent tool breadth, server-wide security headers | cloud and star layers, country mode, satellite trails, category pills, mobile control sheet, Terraform |
| worldmonitor, Shadowbroker, osiris | space-relevant subset only: flights, military flights, earthquakes, traffic, CCTV list, geocode, PWA, agent present; AIS, FIRMS, launches, radio, cables and datacenters, air quality, AI briefing, SAR and SIGINT, mesh, desktop packaging, i18n absent (the last three deliberately) | SSA domain, which none of the three attempt | OSINT breadth |

### 8.4 Answer to "all features, and better than all of them"

On the axis this repository exists for, conjunction detection and collision
probability delivered as a service, ours is ahead of all thirteen non-CARA
references: none of them has a validated multi-method Pc, a CDM reader in both
formats, a CARA harness or a typed agent surface, and the screening benchmark
above shows the full-catalog path working at 12,000 objects. Against CARA itself
we match the 2D methods to CARA's published values but do not have its 3D
methods, its Monte Carlo confidence bounds or its ephemeris path.

On globe and OSINT breadth ours is not the broadest. gods-eye-view, worldmonitor
and keeptrack each carry layers and interaction modes we have not ported. The
CHANGELOG line that claimed every importable capability was implemented has been
corrected, along with two attribution claims for code that does not exist (a
Web Speech voice path and satlas cloud and star layers) and a CLAUDE.md line that
documented AIS and FIRMS keys with no code behind them.

### 8.5 Remaining gaps, prioritized

| Pri | Gap | Source | Rough size |
| --- | --- | --- | --- |
| P1 | 3D-Nc (Hall) Pc for the 29 of 53 encounters outside 2D validity, plus SDMC confidence bounds on `pc_monte_carlo_2d` | CARA | large; the A09 3D reference CSV is already vendored in `tests/data/cara/` |
| P2 | Elrod and PcCircle Gauss-Chebyshev, square and equal-area HBR, DCP cross-correlation | CARA | medium |
| P2 | Satellite name and NORAD search on the globe (search is geocode only, `doSearch`) | ThingsInSpace, OrbitVoyage, satlas | small |
| P2 | AIS, FIRMS and Launch Library 2 layers behind server-side keys | gods-eye-view, worldmonitor | medium each |
| P3 | Clohessy-Wiltshire tradespace and its verification script | satguard | medium |
| P3 | CZML export for external Cesium clients; Python client package | spacetech-ssa, orbveil | small |
| P3 | Anime style, satellite trails, category pills, country mode | gods-eye-view, satlas | small each |
| P3 | CCTV viewshed and gizmo, 3D aircraft models, voice agent, annotations | gods-eye-view | large |
| P4 | Ephemeris-based Pc, OD quality, Delande credibility, VNB transforms | CARA | large |
| P4 | WebGL point renderer at keeptrack scale, WASM SGP4, polar plots, RCS and magnitude estimators | keeptrack | large |
| P4 | Playwright e2e, CodeQL and Dependabot, ADRs, DATA_SOURCES and TESTING docs | keeptrack, satvis, gods-eye-view | small |

## 9. Re-check against the twelve maps (verdict 2026-10-09, evidence rerun 2026-10-10)

The question this time was narrower than section 8: is the application ahead of
every other map in `reference/`? Twelve of the fourteen references draw a map or
a globe. CARA is a MATLAB library and orbveil is an API, so neither is counted
here; section 8.3 still covers them.

The answer is no. Ours is ahead of six, mixed against six, and behind none.

A fresh reviewer on the strong model read the code of all twelve with the
instruction to score against us when unsure, and returned one structured line:
`BEATS_ALL=NO AHEAD=6 MIXED=6 BEHIND=0`. Three of its claims were then checked by
hand: `reference/keeptrack.space/src/plugins` holds 92 entries (87 directories),
the `src/engine/ootk` submodule in that checkout is empty, so keeptrack's own Pc
code could not be read, and the realtime voice, cyclone and sky-view code it
cited in gods-eye-view and satvis is where it said.

### 9.1 Evidence

| Check | Command | Result |
| --- | --- | --- |
| Test suite | `pytest tests/ -q` | 1564 passed, 1 skipped, 29 xfailed in 107 s |
| Lint and types | `ruff check src/ tests/`, `mypy src/sda` | both clean (45 source files); `scripts/generate_preview.py`, outside the CI scope, had 4 UP017 hits on 2026-10-09 |
| CARA reference harness | `python -m sda.cara` | 53/53 Foster cases inside CARA's tolerance bands, 11/11 Alfano literals matched |
| Cross-method Pc validation | `python -m sda.validation` | all checks passed |
| API surface | `python scripts/gen_api_table.py --write` | 73 REST operations + 1 WebSocket; README table unchanged by the rewrite |
| No network from tests | `tests/test_network_guard.py` | 4 passed; a lookup of any host that is not this machine is refused |
| Live catalog | `GET /health` on the launcher's server | 16,081 objects after a live download of 11 CelesTrak groups at 04:43 local on 2026-10-10 |

### 9.2 What moved since section 8

| Section 8.5 row | Status now | Where |
| --- | --- | --- |
| Satellite name and NORAD search on the globe | done, with alpha-5 ids | `static/js/satsearch.js` (alpha-5 decode adapted from satlas, MIT) |
| AIS, FIRMS and Launch Library 2 layers | done as three of ten feed layers; ships and fires each need a free key and say so | `static/js/layers.js`, `routes/worldview.py` |
| Category pills | done, fourteen categories with counts | `static/js/categories.js` (classifier and pills adapted from satlas, MIT) |
| Live camera-feed panel | done for the ISS | `static/js/isscam.js` (feed list and embed parameters adapted from osiris, MIT) |
| Web conjunction browser | partly: Pc method comparison, avoidance options and a breakup preview on the open event; no heatmap | `static/js/conjactions.js` (Pc presentation rules adapted from satguard, MIT) |
| Real-Earth imagery | done, keyless: NASA Blue Marble, day and night with Black Marble city lights, Esri World Imagery | `worldview.html` `setBasemap` |
| Pass prediction on the globe | done | `static/js/passpanel.js` |
| Agent on the globe | done, on a local model by default | `static/js/agentpanel.js` |

Everything else in section 8.5 is unchanged, including all of the CARA rows.

### 9.3 Verdict per map

| Reference | License | Call | Ours ahead on | Theirs ahead on |
| --- | --- | --- | --- | --- |
| satlas | MIT | ahead | conjunction analysis, 73 routes, agent and MCP, constellation filter now matched | cloud and star layers, country mode, mobile control sheet |
| satguard | MIT | ahead | CARA harness, usage checks, CDM XML, a globe at all, passes, breakup, agent | Clohessy-Wiltshire tradespace, conjunction heatmap |
| ThingsInSpace | none stated | ahead | backend, analysis, live refresh; its search box and group filter are now matched | nothing found in its code that ours lacks |
| spacetech-ssa | Apache 2.0 | ahead | screening scale, Pc, CDM, the globe | CZML export, ML residual orbit model |
| OrbitVoyage | none stated | ahead | the whole assessment stack; search is now matched | idle auto-hide panels, WebSocket push of conjunction events |
| worldview (shawn14) | none stated | ahead | SSA domain, tests, worker propagation, security posture | country labels, geopolitical alert zones |
| gods-eye-view | MIT | mixed | the SSA math, full-catalog worker SGP4, tests and CI | weather layers, 3D aircraft models, realtime voice, Anime style, CCTV viewshed |
| keeptrack.space | AGPL 3.0 | mixed | Pc rigor checked against CARA, CDM in both formats, agent and MCP | 87 plugin directories against our ten modules: polar plot, watchlist, stereo map, RPO tools, scenario persistence, renderer scale; its catalog loader accepts OMM columns (section 9.6) |
| worldmonitor | AGPL 3.0 | mixed | SSA domain, which it does not attempt | about 70 layer definitions against our ten feed layers |
| Shadowbroker | AGPL 3.0 | mixed | SSA domain | more than 40 layers, SAR; its satellite fetcher reads OMM fields (section 9.6) |
| osiris | MIT | mixed | SSA domain | 16 layers, recon routes |
| satvis | MIT | mixed | Pc and conjunction analytics, CDM, breakup, agent | phone sky view, pass notifications, a PWA that works on the live app (ours is static-build only), satellites built from OMM JSON (section 9.6) |

keeptrack is scored mixed and not behind because its collision list comes from a
hosted feed and its own Pc code was not in the checkout. For looking at
satellites, as opposed to assessing conjunctions, it is well ahead of ours and
that will not close by copying: it is AGPL, so it is a source of ideas only.

### 9.4 What only this application has

- Monte Carlo Pc beside Foster, Chan and Alfano, with coded usage checks, the
  Frisbee and dilution maxima, and a harness that reproduces NASA CARA's numbers.
- CDM ingestion in KVN and XML, with Pc computed from the covariances in the message.
- Conjunction analysis and live OSINT feeds on the same globe.
- An assistant with typed tools that runs on a local model, and the same tools over MCP.

### 9.5 Remaining gaps, with what may be taken

| Pri | Gap | Donor | License | May copy |
| --- | --- | --- | --- | --- |
| P1 | Objects cataloged since July 2026 (section 9.6) | satvis `src/modules/util/gp.ts` for the browser side; `sgp4.omm` in python-sgp4, already installed, for the server side | MIT | yes |
| P2 | Weather layers (radar, cyclones) | gods-eye-view | MIT | yes |
| P2 | Phone sky view and pass notifications; PWA for the live app | satvis | MIT | yes |
| P2 | Mobile layout, cloud layer, country panel, debris drawn on the globe | satlas | MIT | yes |
| P3 | Clohessy-Wiltshire tradespace and conjunction heatmap | satguard | MIT | yes |
| P3 | 3D aircraft models, realtime voice, Anime style | gods-eye-view | MIT | yes; voice needs a paid realtime API |
| P3 | More OSINT layers | osiris | MIT | yes |
| P3 | More OSINT layers | worldmonitor, Shadowbroker | AGPL 3.0 | no, ideas only |
| P4 | Polar plot, watchlist, stereo map, RPO tools | keeptrack.space | AGPL 3.0 | no, ideas only |
| P1 to P4 | Every CARA row of section 8.5 | CARA | NOSA 1.3 | fixtures only |

### 9.6 The catalog stops in July 2026

Found on 2026-10-10 while checking the live download, not by the reviewer.

CelesTrak's `last-30-days` group, asked for in TLE format, answered HTTP 404 with
a 16 character body, the length of "No GP data found". The `active` group from
the same pass holds 15,941 objects; the highest catalog number in it is 69998 and
the newest launch designator is 2026-159Z, about the middle of July. The public
five-digit catalog numbers ran out around 2026-07-20, and a TLE has no room for
the six-digit numbers issued since, so those objects are published only in the
OMM formats (JSON, CSV, XML, KVN).

This application reads TLEs only (`TLEStore.load_from_text`, `Satrec.twoline2rv`,
and `twoline2satrec` in the browser worker on satellite.js 5.0.0). Every object
cataloged since then is therefore missing from the globe and from conjunction
screening, and the gap grows with each launch.

satvis builds its satellites with `json2satrec` (`src/modules/util/gp.ts`, line
214), so it can show them. keeptrack's catalog loader and Shadowbroker's
satellite fetcher both parse OMM field names; neither was read further than that.
No OMM reader was found in the other nine by a search for the OMM field names
and `FORMAT=json` or `csv` requests; gods-eye-view asks CelesTrak for
`FORMAT=tle` as we do.

What was done here: `last-30-days` is no longer requested (it could only return
an error on every refresh) and is gone from the globe's catalog menu. What is
open: an OMM path through the store, the worker, `rv2tle`, CDM output and the
alpha-5 search. It changes the rule that all ingestion goes through
`TLEStore.load_from_text`, so it waits for a decision.
