---
paths:
  - "src/sda/api.py"
---

# API Rules

- TLE store is module-level singleton — no database dependency
- CelesTrak seed on startup is best-effort (silent failure OK)
- `/conjunctions` runs synchronously — acceptable for catalogs < 100 objects
- All responses use Pydantic models for serialization
- TLE ingestion must go through `TLEStore.load_from_text()` for validation
- Visualization endpoint returns full HTML with CDN-hosted Plotly.js
