"""Benchmark the all-pairs and spatially indexed coarse screens.

Two catalogs are timed:

1. The bundled demo catalog (sda.demo_tles), through the full two-phase
   pipeline, so the numbers include the 1 s fine refinement. Both methods must
   report the same events; the script checks that and says so.
2. A synthetic catalog (5,000 objects by default) built by perturbing the demo
   TLEs and re-ingesting them through TLEStore.load_from_text, timed on phase 1
   only. The full pipeline on 12.5M pairs is not the thing being compared here,
   and the fine phase is identical between the two methods anyway.

Usage:
    .venv/Scripts/python scripts/bench_screening.py
    .venv/Scripts/python scripts/bench_screening.py --objects 2000 --hours 0.5

Output is plain ASCII so it renders on a Windows console.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sda.conjunction import find_conjunctions, screen_coarse  # noqa: E402
from sda.demo_tles import DEMO_TLE_TEXT  # noqa: E402
from sda.screening import SCIPY_AVAILABLE, active_spatial_backend  # noqa: E402
from sda.tle_store import TLEStore  # noqa: E402

BENCH_START = datetime(2026, 1, 1, tzinfo=UTC)


def checksum(stem: str) -> str:
    """Standard TLE modulo-10 checksum over the first 68 columns."""
    total = sum(int(c) for c in stem if c.isdigit()) + stem.count("-")
    return str(total % 10)


def perturb_tle(line1: str, line2: str, new_id: int, seed: int) -> tuple[str, str]:
    """Clone a TLE onto a new NORAD id with a jittered orbit plane and phase.

    Deterministic in ``seed``: the same seed always produces the same object, so
    two runs of this script screen exactly the same catalog.
    """
    inclination = float(line2[8:16])
    ecc_digits = line2[26:33]
    arg_perigee = float(line2[34:42])
    mean_motion = float(line2[52:63])

    raan = (seed * 137.508) % 360.0
    mean_anomaly = (seed * 73.398) % 360.0
    inclination = min(179.9, max(0.1, inclination + ((seed % 41) - 20) * 0.1))
    mean_motion = mean_motion * (1.0 + ((seed % 101) - 50) * 0.0001)

    stem1 = f"1 {new_id:05d}U {line1[9:17]}" + line1[17:68]
    stem2 = (
        f"2 {new_id:05d} {inclination:8.4f} {raan:8.4f} {ecc_digits} "
        f"{arg_perigee:8.4f} {mean_anomaly:8.4f} {mean_motion:11.8f}{1:5d}"
    )
    return stem1 + checksum(stem1), stem2 + checksum(stem2)


def build_synthetic_store(n_objects: int) -> TLEStore:
    """A catalog of ``n_objects`` perturbed demo TLEs, ingested the normal way."""
    seed_store = TLEStore()
    seed_store.load_from_text(DEMO_TLE_TEXT)
    seeds = seed_store.get_all()

    blocks: list[str] = []
    for i in range(n_objects):
        base = seeds[i % len(seeds)]
        new_id = 70000 + i
        line1, line2 = perturb_tle(base.line1, base.line2, new_id, i + 1)
        blocks.append(f"SYNTH {new_id}\n{line1}\n{line2}")

    store = TLEStore()
    ingested = store.load_from_text("\n".join(blocks))
    if ingested != n_objects:
        print(f"  note: {n_objects - ingested} synthetic TLEs were rejected by SGP4")
    return store


def bench_demo_pipeline(hours: float, threshold_km: float) -> None:
    """Full two-phase pipeline on the demo catalog, both methods."""
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    n = len(store.get_all())
    print(f"Demo catalog: {n} objects, {hours} h window, threshold {threshold_km} km")
    print("  (full pipeline: 60 s coarse screen plus 1 s fine refinement)")

    results = {}
    for method in ("allpairs", "spatial"):
        t0 = time.perf_counter()
        events = find_conjunctions(
            store, hours=hours, threshold_km=threshold_km, start=BENCH_START, method=method
        )
        elapsed = time.perf_counter() - t0
        results[method] = events
        print(f"  {method:9s} {elapsed * 1000:9.1f} ms   {len(events):4d} events")

    pairs_a = {(e.primary, e.secondary) for e in results["allpairs"]}
    pairs_b = {(e.primary, e.secondary) for e in results["spatial"]}
    print(f"  identical event set: {pairs_a == pairs_b}")


def bench_synthetic_screen(n_objects: int, hours: float, threshold_km: float) -> None:
    """Phase 1 only on a large synthetic catalog, both methods."""
    coarse_margin = threshold_km * 10
    print()
    print(f"Synthetic catalog: {n_objects} objects, {hours} h window")
    print(f"  (phase 1 only, coarse margin {coarse_margin} km)")

    t0 = time.perf_counter()
    store = build_synthetic_store(n_objects)
    build_seconds = time.perf_counter() - t0
    tles = store.get_all()
    print(f"  catalog build {build_seconds:6.2f} s ({len(tles)} objects ingested)")

    results = {}
    for method in ("allpairs", "spatial"):
        t0 = time.perf_counter()
        candidates, _times, resolved = screen_coarse(
            tles, BENCH_START, hours=hours, coarse_margin_km=coarse_margin, method=method
        )
        elapsed = time.perf_counter() - t0
        results[method] = {(c[0], c[1]) for c in candidates}
        print(f"  {resolved:9s} {elapsed:9.2f} s    {len(candidates):6d} candidate pairs")

    print(f"  identical candidate set: {results['allpairs'] == results['spatial']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objects", type=int, default=5000, help="synthetic catalog size")
    parser.add_argument("--hours", type=float, default=1.0, help="synthetic window, hours")
    parser.add_argument("--demo-hours", type=float, default=6.0, help="demo window, hours")
    parser.add_argument("--threshold-km", type=float, default=10.0, help="reporting threshold")
    args = parser.parse_args()

    backend = active_spatial_backend()
    print(f"Spatial backend: {backend} (scipy installed: {SCIPY_AVAILABLE})")
    print()
    bench_demo_pipeline(args.demo_hours, args.threshold_km)
    bench_synthetic_screen(args.objects, args.hours, args.threshold_km)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
