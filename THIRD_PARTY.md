# Third-party code and data

This project is MIT licensed (see `LICENSE`). Where another project already solved a
problem, its code or design was adapted instead of being rewritten, and every such
piece is listed here with the place it came from. The rule that governs what may be
taken:

- MIT, BSD and Apache-2.0 sources may be adapted into this codebase with their notice.
- AGPL and GPL projects (keeptrack.space, worldmonitor, Shadowbroker) are read for
  ideas only. No code from them is in this repository.
- Projects with no license file (ThingsInSpace, OrbitVoyage, shawn14/worldview) are
  all rights reserved. They are read for comparison only.
- NASA CARA is under the NASA Open Source Agreement 1.3. Its test fixtures are vendored
  verbatim with their notice; none of its MATLAB code is translated here.

The donor checkouts live under `reference/` (not committed; `reference/README.md` has
the inventory and the commit each was read at). "Commit read" below is the donor commit
the adaptation was made from.

## Adapted code

| Our file | Donor | Donor path | Commit read | License | What was taken |
| --- | --- | --- | --- | --- | --- |
| `src/sda/templates/static/js/styles.js` | [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | `src/styles/` | 65bc522 | MIT | Shaders and the stage-registry design for the CRT, night vision, FLIR, noir and snow styles, rewritten to the GLSL subset that runs under both ES 1.00 and 3.00 |
| `src/sda/templates/static/js/tactical.js` | bilawalsidhu/gods-eye-view | trail renderer, label arbiter, tracked camera | 65bc522 | MIT | The design of the fading trails, the screen-space label arbiter and the tracked-camera poses |
| `src/sda/ratelimit.py` | bilawalsidhu/gods-eye-view | server rate limiter | 65bc522 | MIT | Per-IP keying, the refusal to trust client forwarding headers by default, and the bounded key map. The token bucket and the FastAPI integration are ours |
| `src/sda/agent.py` (`_view_band`, `_region_label`, `_tool_summarize_scene`) | bilawalsidhu/gods-eye-view | agent HUD summary | 65bc522 | MIT | Camera standoff bands, coarse region names and the rolling scene summary handed to the model |
| `src/sda/templates/static/js/sgp4-worker.js` | [Flowm/satvis](https://github.com/Flowm/satvis) | `src/modules/util/sgp4Worker.ts` | 06a74fe | MIT | Worker design: a satrec cache as the only state, each request a pure function of its timestamp, one packed typed array per reply, transferable buffers handed back |
| `src/sda/templates/static/sw.js` | Flowm/satvis | PWA configuration | 06a74fe | MIT | The precache-the-shell approach for the static demo |
| `src/sda/passes.py` (`compass_point`, pass quality) | Flowm/satvis | `PassPredictor` | 06a74fe | MIT | The 16-point compass mapping and the 0 to 1 pass quality score |
| `src/sda/passes.py` (`predict_passes`) | [PremaanshVyas/satlas](https://github.com/PremaanshVyas/satlas) | `predict_passes` | f170fef | MIT | The rise, culminate, set structure of a pass. The search itself is ours |
| `src/sda/screening.py` | [cesabici-bit/satguard](https://github.com/cesabici-bit/satguard) | screening module | 07a86ce | MIT | The apogee and perigee shell prefilter and the idea of a per-step spatial index over vectorized SGP4 output, reimplemented on this project's models |
| `src/sda/conjunction.py` (vectorized screen) | cesabici-bit/satguard | vectorized screen | 07a86ce | MIT | The structure of the vectorized coarse screen |
| `src/sda/probability.py` (`regime_default_covariance_rtn`, realism scaling) | cesabici-bit/satguard | `covariance/realism.py` | 07a86ce | MIT | Regime default covariance structure, extended here with a HEO row |
| `src/sda/screening.py` (formations, composite score), `src/sda/conjunction.py` (regime survey) | [ncdrone/orbveil](https://github.com/ncdrone/orbveil) | screening and survey modules | 6fd454e | Apache-2.0 | The formation exclusion table and the composite risk score design, implemented here from the design |
| `src/sda/cdm.py` (`_parse_cdm_xml`) | ncdrone/orbveil | `data/cdm.py` | 6fd454e | Apache-2.0 | The structure of the CCSDS CDM XML reader |
| `src/sda/routes/worldview.py` (`_swpc_kp`) | [simplifaisoul/osiris](https://github.com/simplifaisoul/osiris) | `src/app/api/space-weather/route.ts` | 301e697 | MIT | The `Kp` / `kp_index` key fallback for NOAA's K-index rows |
| `src/sda/templates/static/js/isscam.js` | simplifaisoul/osiris | `src/components/SpaceCam.tsx` | 8edb376 | MIT | The three ISS camera feeds (YouTube video ids, labels, descriptions), the youtube-nocookie embed parameters, the nominal 408 km and 93 minute figures, and the idea of a source link and an enlarge control beside the player. The plain-script panel, the floating frame, the id validation, the Find the ISS button and the selection hook are ours. Feed ids answered HTTP 200 on YouTube's oEmbed endpoint on 2026-10-09 |
| `src/sda/templates/static/js/categories.js` | PremaanshVyas/satlas | `apps/web/src/globe/Globe.ts` (`classifySatellite`, `SatCategory`), `apps/web/src/components/MobileControlsSheet.tsx` | 410f923 | MIT | The name-prefix classifier rules for Starlink, GPS and Iridium, the debris markers, the Starlink / GPS / Iridium / Debris / Other categories with their labels, and the multi-select pill toggle. Added here: OneWeb, Kuiper, Qianfan/Guowang, GLONASS, Galileo, BeiDou, Stations, Weather and Geostationary (by regime), debris tested first and matched as a whole word, an All pill, counts, and the hide and show logic against the globe |
| `src/sda/templates/static/js/conjactions.js` | cesabici-bit/satguard | `web/src/components/ConjunctionBrowser.tsx`, `web/src/components/ObjectInspector.tsx` | 07a86ce | MIT | The Pc presentation rules only: bands at strictly above 1e-4 and above 1e-6, Pc as `toExponential` with N/A for a missing value, the truncate helper and the compact badge-plus-numbers row, restyled in this page's risk colours. The request builders, the panel, the fragment cloud and its lifecycle are ours |
| `src/sda/templates/static/js/satsearch.js` (`noradToInt`, `noradKey`) | PremaanshVyas/satlas | `apps/web/src/lib/norad.ts` | 410f923 | MIT | The alpha-5 alphabet and the decode and key functions for NORAD ids above 99,999, made stricter here (an upper-case letter and exactly four digits) |

Copyright holders, from each donor's `LICENSE`: gods-eye-view, Copyright (c) 2026 Bilawal
Sidhu; satvis, Copyright (c) 2018 Florian Mauracher; satlas, Copyright (c) 2026 Premaansh
Vyas; satguard, Copyright (c) 2026 Genius Lab; osiris, Copyright (c) 2026 simplifaisoul;
orbveil, Apache License 2.0 (no separate copyright line in its `LICENSE`).

### MIT permission notice

The five MIT donors above (gods-eye-view, satvis, satlas, satguard, osiris) each ship this
text under their own copyright line, quoted in the paragraph before this one. It is
reproduced once here and applies to the portions taken from each of them.

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

orbveil is under the Apache License 2.0 (<https://www.apache.org/licenses/LICENSE-2.0>).
What was taken from it was a design, implemented here in new code, and its repository
carries no `NOTICE` file.

## Vendored data

| Path | Source | License | Notes |
| --- | --- | --- | --- |
| `tests/data/cara/` | [nasa/CARA_Analysis_Tools](https://github.com/nasa/CARA_Analysis_Tools) | NASA Open Source Agreement 1.3 | 53 reference CDMs, the `PcMethodTestConjunctions` values and the Alfano 2009 cases, verbatim, with `NOTICE.md` and one agreement PDF alongside. Imported by `scripts/import_cara_fixtures.py` |

## Runtime services and libraries loaded by the page

These are fetched by the browser or proxied by the server at run time; none is bundled.

| What | Where from | Terms |
| --- | --- | --- |
| CesiumJS 1.119 | jsDelivr | Apache-2.0 |
| satellite.js 5.0.0 | jsDelivr | MIT |
| Blue Marble and Black Marble imagery | NASA GIBS | Public, credit to NASA |
| World Imagery | Esri ArcGIS Online | Esri terms, attribution shown under the basemap buttons |
| Element sets | CelesTrak, optionally Space-Track | CelesTrak asks for no more than one download of a group per two-hour update window; the server enforces that when `SDA_TLE_CACHE_DIR` is set |
| Space weather | NOAA SWPC | Public domain |
