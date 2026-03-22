# Space-Domain Awareness Collision Predictor

**Real-time satellite conjunction analysis engine** — SGP4 orbital propagation, two-phase collision detection, risk classification, and interactive 3D visualization.

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-1.0-009688?logo=fastapi&logoColor=white)
![SGP4](https://img.shields.io/badge/SGP4-WGS72-orange)

---

## What It Does

This system ingests **Two-Line Element sets (TLEs)** from sources like CelesTrak, propagates satellite orbits using the **SGP4** algorithm, and detects potential collisions between tracked objects.

### Key Capabilities

- **SGP4 Propagation** — Accurate orbital prediction using the WGS72 gravity model, with vectorized NumPy batch processing
- **Two-Phase Conjunction Detection** — Coarse screening (60s steps) across all satellite pairs, then fine refinement (1s steps) around candidate close approaches
- **Risk Classification** — Five-level threat assessment (CRITICAL / HIGH / MODERATE / LOW / NEGLIGIBLE) based on miss distance and relative velocity
- **Interactive 3D Visualization** — Plotly-powered Earth + orbit traces + conjunction markers with hover details
- **Web Dashboard** — Real-time mission control interface with satellite catalog, conjunction analysis, and embedded 3D view
- **REST API** — 7 endpoints for TLE ingestion, satellite tracking, conjunction analysis, and visualization

---

## Quick Start

```bash
# Clone and set up
git clone <repo-url>
cd space-collision-predictor

# Create virtual environment
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# Install dependencies
pip install -e ".[dev]"

# Launch the server
python -m sda.api
```

Open **http://localhost:8000** to access the dashboard. The server automatically seeds satellite data from CelesTrak on startup.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Web Dashboard (/)                      │
│  Satellite Catalog │ Conjunction Events │ 3D Orbit View  │
└──────────────────────────┬──────────────────────────────┘
                           │ REST API
┌──────────────────────────┴──────────────────────────────┐
│                     FastAPI Service                      │
│  /health  /satellites  /tle  /conjunctions  /visualize   │
└──┬───────────┬────────────┬────────────┬────────────────┘
   │           │            │            │
┌──┴──┐   ┌───┴───┐   ┌────┴────┐   ┌───┴────┐
│ TLE │   │  SGP4  │   │ Conj.   │   │ Plotly │
│Store│   │Propag. │   │Pipeline │   │  Viz   │
└─────┘   └────────┘   └─────────┘   └────────┘
```

| Module | Purpose |
|--------|---------|
| `models.py` | Pydantic data models (TLERecord, StateVector, ConjunctionEvent, RiskLevel) |
| `propagator.py` | SGP4 engine wrapper — ECI propagation, Julian date handling, vectorized NumPy mode |
| `conjunction.py` | Two-phase detection pipeline + risk classification |
| `tle_store.py` | In-memory TLE catalog with 2-line/3-line parser |
| `visualization.py` | Plotly 3D Earth, orbit traces, conjunction markers |
| `dashboard.py` | Embedded web dashboard HTML |
| `api.py` | FastAPI service with 7 endpoints + CelesTrak seed |

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web dashboard |
| `GET` | `/health` | System health + satellite count |
| `GET` | `/satellites` | List all tracked satellites |
| `GET` | `/satellites/{norad_id}` | Satellite detail + current state vector |
| `POST` | `/tle` | Ingest TLE text |
| `DELETE` | `/satellites/{norad_id}` | Remove from tracking |
| `POST` | `/conjunctions` | Run conjunction analysis |
| `GET` | `/conjunctions/visualize` | Interactive 3D visualization |

Auto-generated API docs available at **/docs** (Swagger UI) and **/redoc**.

---

## Risk Classification

| Miss Distance | Relative Velocity | Risk Level |
|:---:|:---:|:---:|
| < 0.5 km | any | **CRITICAL** |
| < 1.0 km | any | **HIGH** |
| < 5.0 km | > 10 km/s | **HIGH** |
| < 5.0 km | ≤ 10 km/s | **MODERATE** |
| < 10.0 km | any | **LOW** |
| ≥ 10.0 km | any | **NEGLIGIBLE** |

---

## Testing

```bash
# Run all tests
pytest tests/ -x --tb=short

# With coverage
pytest tests/ --cov=src/sda

# Quick smoke test
pytest tests/ -q
```

---

## Tech Stack

- **Python 3.11+** — Core language
- **sgp4** — NORAD SGP4/SDP4 orbital propagation (WGS72)
- **NumPy** — Vectorized orbital computations
- **FastAPI** — Async REST API with auto-generated OpenAPI docs
- **Plotly** — Interactive 3D orbit visualization
- **Pydantic** — Data validation and serialization
- **httpx** — Async HTTP client for CelesTrak integration

---

## License

MIT
