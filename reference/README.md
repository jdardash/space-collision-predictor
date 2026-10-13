# Reference checkouts

Third-party projects cloned here for capability comparison against this
repository. The clones themselves are ignored by git (`reference/*/` in
`.gitignore`); only this file and the comparison notes are tracked. Every clone
is `--depth 1`, so the pinned commit below is the only history present.

Re-fetch any of them with:

```bash
cd reference && git clone --depth 1 https://github.com/<owner>/<repo>.git <dir>
```

Comparison results: [COMPARISON.md](COMPARISON.md).

## Inventory (cloned 2026-09-02; the five MIT clones we take code from were pulled 2026-10-09)

| Dir | Repo | Pinned | Stars | License | Stack | Why it is here |
| --- | --- | --- | ---: | --- | --- | --- |
| `gods-eye-view/` | [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | 95fa816 (2026-10-07) | 48.9k | MIT | Vite + CesiumJS, vanilla JS | The OSINT globe this project's WorldView was modelled on. Primary comparison target. |
| `keeptrack.space/` | [thkruz/keeptrack.space](https://github.com/thkruz/keeptrack.space) | 15a14f1 (2026-08-08) | 1.6k | AGPL-3.0 | TypeScript, WebGL | Most mature open-source space situational awareness app: sensors, lookangles, breakups, close-approach screening. |
| `worldmonitor/` | [koala73/worldmonitor](https://github.com/koala73/worldmonitor) | ec335cd (2026-09-02) | 85k | AGPL-3.0 | TypeScript, Tauri | The "open-source Palantir" intelligence dashboard. Breadth-of-layers benchmark. |
| `Shadowbroker/` | [BigBodyCobain/Shadowbroker](https://github.com/BigBodyCobain/Shadowbroker) | cd6395f (2026-08-23) | 11k | AGPL-3.0 | Python | OSINT for jets, spy satellites, seismic and more. |
| `osiris/` | [simplifaisoul/osiris](https://github.com/simplifaisoul/osiris) | 8edb376 (2026-10-08) | 10.6k | MIT | Next.js, MapLibre | "Palantir alternative" OSINT dashboard; the parent of the many osiris forks. |
| `ThingsInSpace/` | [jeyoder/ThingsInSpace](https://github.com/jeyoder/ThingsInSpace) | 4f56a32 (2019-12-15) | 834 | none | JavaScript, WebGL | stuffin.space, the original real-time full-catalog globe. Archived-quality baseline. |
| `satvis/` | [Flowm/satvis](https://github.com/Flowm/satvis) | 1a63cc0 (2026-10-08, branch `main`; `next` is gone upstream) | 409 | MIT | TypeScript, Vue, CesiumJS | 12,000+ satellite tracker and pass predictor on Cesium; closest rendering peer. |
| `CARA_Analysis_Tools/` | [nasa/CARA_Analysis_Tools](https://github.com/nasa/CARA_Analysis_Tools) | 86ba28a (2026-08-06) | 110 | NASA OSA (see repo) | MATLAB | NASA Conjunction Assessment Risk Analysis Pc reference implementations and test vectors. |
| `spacetech-ssa/` | [IBM/spacetech-ssa](https://github.com/IBM/spacetech-ssa) | 26986e5 (2021-02-21) | 122 | Apache-2.0 | Python | IBM ML-augmented orbit prediction for SSA. |
| `satlas/` | [PremaanshVyas/satlas](https://github.com/PremaanshVyas/satlas) | 410f923 (2026-09-10) | 31 | MIT | TypeScript | Real-time SSA with an AI agent front door. |
| `satguard/` | [cesabici-bit/satguard](https://github.com/cesabici-bit/satguard) | 07a86ce (2026-03-23) | 4 | MIT | Python, CesiumJS | Direct competitor: TLE ingest, SGP4, Foster/Chan/Alfano Pc, Cesium globe. |
| `orbveil/` | [ncdrone/orbveil](https://github.com/ncdrone/orbveil) | 6fd454e (2026-02-11) | 1 | Apache-2.0 | Python | Direct competitor: full-catalog conjunction screening engine. |
| `OrbitVoyage/` | [sparshsharmadev/OrbitVoyage](https://github.com/sparshsharmadev/OrbitVoyage) | a076f0e (2026-05-12) | 2 | none | TypeScript | Space traffic control dashboard with conjunction prediction. |
| `worldview/` | [shawn14/worldview](https://github.com/shawn14/worldview) | 9a48c4e (2026-03-02) | 3 | none | TypeScript, CesiumJS | Independent gods-eye-view style clone (Google 3D tiles, aircraft, satellites). |

Repos without a LICENSE file (ThingsInSpace, OrbitVoyage, worldview) are
all-rights-reserved by default: read for comparison only, do not copy code.
AGPL-3.0 repos (keeptrack.space, worldmonitor, Shadowbroker) cannot be mixed
into this MIT codebase without relicensing; treat them as design references.

Searches that produced this list (GitHub, 2026-09-02): "spy satellite",
"space situational awareness", "satellite conjunction", "keeptrack",
"worldmonitor", "satellite tracking cesium globe", "OSIRIS palantir".
Candidates seen and deliberately skipped: `JamesDavid/Overhead` and
`Stugre/crowpanel-800x480-radar-satellites` (ESP32 hardware dashboards),
`shashwatak/satellite-js` (a library this project already uses, not an app),
`ARCLab-MIT/MOCAT-SSEM` (population evolution model, not conjunction
assessment), the osiris forks (all point back to `simplifaisoul/osiris`).
