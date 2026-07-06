# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Full-covariance collision probability: Foster 2D quadrature (`pc_foster`), Chan analytic series (`pc_chan`), and 2D Monte Carlo (`pc_monte_carlo_2d`) over real 3x3 position covariances, with RTN-to-ECI covariance rotation and encounter-plane projection (`compute_collision_probability_full`)
- CCSDS CDM ingestion: KVN parser (`parse_cdm`) honoring CCSDS 508.0-B-1 default units with bracketed-unit overrides, and Pc computed from the message's per-object RTN covariances (`compute_pc_from_cdm`) — CDM support is now two-directional
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

- Collision probability now uses TLE-age-scaled position covariance (Vallado 2013, Ch. 9) instead of a fixed 50 m sigma — stale TLEs no longer produce overconfident Pc values
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
